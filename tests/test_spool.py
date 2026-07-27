from __future__ import annotations

import errno
import json
import os
import stat
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID

import pytest

from monitor_agent.models import RetentionResult, SpoolStats
from monitor_agent.spool import Spool


def payload(event_id: str, **extra: object) -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "event_id": event_id,
        "event": "heartbeat",
        **extra,
    }


def event(number: int) -> str:
    return f"00000000-0000-4000-8000-{number:012d}"


def test_constructor_validates_bounds_and_creates_owner_only_directories(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="max_bytes must be positive"):
        Spool(tmp_path / "bytes", max_bytes=0, max_age_sec=1)
    with pytest.raises(ValueError, match="max_age_sec must be positive"):
        Spool(tmp_path / "age", max_bytes=1, max_age_sec=0)

    root = tmp_path / "spool"
    spool = Spool(root, max_bytes=1, max_age_sec=1)

    assert spool.root == root
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    assert stat.S_IMODE((root / "dead-letter").stat().st_mode) == 0o700


def test_enqueue_is_owner_only_and_oldest_first_with_compact_unicode_json(tmp_path: Path) -> None:
    spool = Spool(tmp_path, max_bytes=1_048_576, max_age_sec=3600)
    first = spool.enqueue(payload(event(1), message="Merhaba 🌍"))
    second = spool.enqueue(payload(event(2)))

    assert spool.pending() == [first, second]
    assert stat.S_IMODE(first.stat().st_mode) == 0o600
    assert first.read_text(encoding="utf-8") == (
        '{"event":"heartbeat","event_id":"00000000-0000-4000-8000-000000000001",'
        '"message":"Merhaba 🌍","schema_version":"1.0"}'
    )
    assert spool.load(first) is not None
    assert spool.load(first)["event_id"].endswith("0001")
    assert not list(tmp_path.glob("*.tmp"))


def test_enqueue_uses_utc_timestamp_and_deterministic_tie_order(tmp_path: Path) -> None:
    spool = Spool(tmp_path, max_bytes=1_048_576, max_age_sec=3600)
    now = datetime(2026, 7, 20, 15, 0, tzinfo=timezone(timedelta(hours=3)))
    later_event_id = event(2)
    first = spool.enqueue(payload(later_event_id), now=now)
    second = spool.enqueue(payload(event(1)), now=now)

    assert first.name.startswith("20260720T120000000000Z_")
    assert spool.pending() == [second, first]


@pytest.mark.parametrize(
    ("bad_payload", "message"),
    [
        ([], "payload must be a mapping"),
        ({"event_id": 1}, "event_id must be a UUID"),
        ({"event_id": "not-a-uuid"}, "event_id must be a UUID"),
        ({"event_id": event(1), "value": object()}, "payload is not JSON serializable"),
    ],
)
def test_enqueue_rejects_invalid_input_without_artifacts(
    tmp_path: Path, bad_payload: object, message: str
) -> None:
    spool = Spool(tmp_path, max_bytes=1_048_576, max_age_sec=3600)

    with pytest.raises((TypeError, ValueError), match=message):
        spool.enqueue(bad_payload)  # type: ignore[arg-type]

    assert spool.pending() == []
    assert not list(tmp_path.glob("*.tmp"))


def test_enqueue_rejects_naive_now_without_artifacts(tmp_path: Path) -> None:
    spool = Spool(tmp_path, max_bytes=1_048_576, max_age_sec=3600)
    with pytest.raises(ValueError, match="now must be timezone-aware"):
        spool.enqueue(payload(event(1)), now=datetime(2026, 7, 20, 12, 0))
    assert spool.pending() == []


def test_enqueue_cleans_temporary_file_when_atomic_publish_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spool = Spool(tmp_path, max_bytes=1_048_576, max_age_sec=3600)

    def fail_link(source: object, destination: object, **kwargs: object) -> None:
        raise OSError("publish failed")

    monkeypatch.setattr("monitor_agent.spool.os.link", fail_link)
    with pytest.raises(OSError, match="publish failed"):
        spool.enqueue(payload(event(1)))

    assert spool.pending() == []
    assert not list(tmp_path.glob("*.tmp"))
    assert not list(tmp_path.glob("*.json"))


def test_enqueue_preserves_an_existing_name_with_a_collision_suffix(tmp_path: Path) -> None:
    spool = Spool(tmp_path, max_bytes=1_048_576, max_age_sec=3600)
    now = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)
    first = spool.enqueue(payload(event(1)), now=now)
    second = spool.enqueue(payload(event(1)), now=now)

    assert first.read_text(encoding="utf-8") == second.read_text(encoding="utf-8")
    assert second.name == f"{first.stem}.1.json"


def test_load_missing_and_reject_missing_without_prior_dead_letter_are_safe(tmp_path: Path) -> None:
    spool = Spool(tmp_path, max_bytes=1_048_576, max_age_sec=3600)
    missing = tmp_path / "20260720T120000000000Z_00000000-0000-4000-8000-000000000001.json"

    assert spool.load(missing) is None
    assert spool.reject(missing).name.endswith(".rejected.json")


def test_constructor_rejects_a_symlinked_spool_root(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ValueError, match="spool directory must be a real directory"):
        Spool(root, max_bytes=1, max_age_sec=1)


def test_corrupt_nonmapping_or_missing_event_id_moves_to_dead_letter(tmp_path: Path) -> None:
    spool = Spool(tmp_path, max_bytes=1_048_576, max_age_sec=3600)
    broken = tmp_path / "20260720T120000000000Z_broken.json"
    not_mapping = tmp_path / "20260720T120000000001Z_scalar.json"
    missing_id = tmp_path / "20260720T120000000002Z_missing.json"
    broken.write_text("{", encoding="utf-8")
    not_mapping.write_text("[]", encoding="utf-8")
    missing_id.write_text(json.dumps({"event": "heartbeat"}), encoding="utf-8")

    assert spool.load(broken) is None
    assert spool.load(not_mapping) is None
    assert spool.load(missing_id) is None
    assert spool.pending() == []
    assert len(list((tmp_path / "dead-letter").glob("*.json"))) == 3


def test_load_invalid_uuid_moves_to_dead_letter_and_reject_has_suffix(tmp_path: Path) -> None:
    spool = Spool(tmp_path, max_bytes=1_048_576, max_age_sec=3600)
    invalid = tmp_path / "20260720T120000000000Z_invalid.json"
    invalid.write_text(json.dumps({"event_id": "invalid"}), encoding="utf-8")
    assert spool.load(invalid) is None

    record = spool.enqueue(payload(event(1)))
    rejected = spool.reject(record)
    assert rejected.parent == tmp_path / "dead-letter"
    assert rejected.name.endswith(".rejected.json")
    assert json.loads(rejected.read_text(encoding="utf-8"))["event_id"] == event(1)
    assert spool.stats().dead_letter_count == 2


def test_ack_and_reject_are_idempotent_for_records(tmp_path: Path) -> None:
    spool = Spool(tmp_path, max_bytes=1_048_576, max_age_sec=3600)
    record = spool.enqueue(payload(event(1)))
    spool.ack(record)
    spool.ack(record)
    assert not record.exists()

    record = spool.enqueue(payload(event(2)))
    rejected = spool.reject(record)
    assert spool.reject(record) == rejected
    assert rejected.exists()


def test_path_operations_reject_outside_records_and_symlinks(tmp_path: Path) -> None:
    spool = Spool(tmp_path / "spool", max_bytes=1_048_576, max_age_sec=3600)
    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps(payload(event(1))), encoding="utf-8")
    symlink = spool.root / "linked.json"
    try:
        symlink.symlink_to(outside)
    except OSError as error:
        pytest.skip(f"symlinks unavailable: {error}")

    for path in (outside, symlink, spool.root / "dead-letter" / "x.json"):
        with pytest.raises(ValueError, match="not a pending spool record"):
            spool.load(path)
        with pytest.raises(ValueError, match="not a pending spool record"):
            spool.ack(path)
        with pytest.raises(ValueError, match="not a pending spool record"):
            spool.reject(path)
    assert outside.exists()


def test_path_operations_reject_pending_directories(tmp_path: Path) -> None:
    spool = Spool(tmp_path, max_bytes=1_048_576, max_age_sec=3600)
    directory = tmp_path / "not-a-record.json"
    directory.mkdir()
    with pytest.raises(ValueError, match="not a pending spool record"):
        spool.ack(directory)


def test_dead_letter_collision_preserves_each_record(tmp_path: Path) -> None:
    spool = Spool(tmp_path, max_bytes=1_048_576, max_age_sec=3600)
    record = spool.enqueue(payload(event(1)))
    target = tmp_path / "dead-letter" / f"{record.stem}.rejected.json"
    target.write_text("existing", encoding="utf-8")

    rejected = spool.reject(record)

    assert target.read_text(encoding="utf-8") == "existing"
    assert rejected != target
    assert json.loads(rejected.read_text(encoding="utf-8"))["event_id"] == event(1)


def test_retention_uses_logical_record_time_for_historical_enqueue(tmp_path: Path) -> None:
    spool = Spool(tmp_path, max_bytes=1_048_576, max_age_sec=60)
    now = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)
    old = spool.enqueue(payload(event(1)), now=now - timedelta(seconds=61))
    fresh = spool.enqueue(payload(event(2)), now=now - timedelta(seconds=59))
    old_size = old.stat().st_size

    result = spool.enforce_retention(now=now)

    assert result == RetentionResult(evicted_count=1, evicted_bytes=old_size)
    assert spool.pending() == [fresh]


def test_retention_evicts_oldest_records_by_size_and_returns_exact_bytes(tmp_path: Path) -> None:
    spool = Spool(tmp_path, max_bytes=1_048_576, max_age_sec=3600)
    base = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)
    first = spool.enqueue(payload(event(1), text="a"), now=base)
    second = spool.enqueue(payload(event(2), text="bbbb"), now=base + timedelta(microseconds=1))
    first_size = first.stat().st_size
    # Enqueue enforces retention, so tighten the bound only for this explicit pass.
    spool.max_bytes = second.stat().st_size
    result = spool.enforce_retention(now=base)

    assert result == RetentionResult(evicted_count=1, evicted_bytes=first_size)
    assert spool.pending() == [second]
    assert spool.stats().pending_bytes == second.stat().st_size


def test_stats_excludes_dead_letters_and_counts_regular_pending_files(tmp_path: Path) -> None:
    spool = Spool(tmp_path, max_bytes=1_048_576, max_age_sec=3600)
    record = spool.enqueue(payload(event(1)))
    rejected = spool.reject(record)
    pending = spool.enqueue(payload(event(2)))

    assert spool.stats() == SpoolStats(
        pending_count=1,
        pending_bytes=pending.stat().st_size,
        dead_letter_count=1,
    )
    assert rejected.exists()


def test_pending_uses_mtime_fallback_for_malformed_record_names(tmp_path: Path) -> None:
    spool = Spool(tmp_path, max_bytes=1_048_576, max_age_sec=60)
    malformed = tmp_path / "not-a-timestamp.json"
    malformed.write_text(json.dumps(payload(event(1))), encoding="utf-8")
    logical_now = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)
    old_epoch = (logical_now - timedelta(seconds=61)).timestamp()
    os.utime(malformed, (old_epoch, old_epoch))

    assert spool.pending() == [malformed]
    assert spool.enforce_retention(now=logical_now).evicted_count == 1


def test_public_operations_are_thread_safe(tmp_path: Path) -> None:
    spool = Spool(tmp_path, max_bytes=1_048_576, max_age_sec=3600)
    with ThreadPoolExecutor(max_workers=8) as executor:
        records = list(
            executor.map(lambda number: spool.enqueue(payload(event(number))), range(1, 41))
        )

    assert len(records) == 40
    assert len(spool.pending()) == 40
    assert [UUID(spool.load(record)["event_id"]) for record in records]  # type: ignore[index]


def test_directory_fsync_is_skipped_off_posix_and_used_on_posix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import monitor_agent.spool as spool_module

    fsync_calls: list[int] = []
    monkeypatch.setattr(spool_module.os, "fsync", lambda descriptor: fsync_calls.append(descriptor))
    monkeypatch.setattr(spool_module, "_supports_directory_fsync", lambda: False)
    Spool(tmp_path / "windows", max_bytes=1_048_576, max_age_sec=3600).enqueue(payload(event(1)))
    assert len(fsync_calls) == 1

    fsync_calls.clear()
    monkeypatch.setattr(spool_module, "_supports_directory_fsync", lambda: True)
    Spool(tmp_path / "posix", max_bytes=1_048_576, max_age_sec=3600).enqueue(payload(event(2)))
    assert len(fsync_calls) == 2


def test_dead_letter_move_failure_keeps_pending_record_and_cleans_reservation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import monitor_agent.spool as spool_module

    spool = Spool(tmp_path, max_bytes=1_048_576, max_age_sec=3600)
    record = spool.enqueue(payload(event(1)))

    def failing_replace(source: object, destination: object, **kwargs: object) -> None:
        raise OSError("fail")

    monkeypatch.setattr(spool_module.os, "replace", failing_replace)
    with pytest.raises(OSError, match="fail"):
        spool.reject(record)
    assert record.exists()
    assert not list((tmp_path / "dead-letter").glob(".move-*.tmp"))


@pytest.mark.parametrize("failure", ["file_fsync", "chmod", "publish"])
def test_enqueue_never_exposes_partial_pending_records_before_publish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    import monitor_agent.spool as spool_module

    spool = Spool(tmp_path, max_bytes=1_048_576, max_age_sec=3600)
    if failure == "file_fsync":
        monkeypatch.setattr(
            spool_module.os, "fsync", lambda _: (_ for _ in ()).throw(OSError("fsync"))
        )
    elif failure == "chmod":
        original_chmod = spool_module.os.chmod

        def fail_staged_chmod(path: object, mode: object, *args: object, **kwargs: object) -> None:
            if Path(path).name.startswith(".spool-"):
                raise OSError("chmod")
            original_chmod(path, mode, *args, **kwargs)

        monkeypatch.setattr(spool_module.os, "chmod", fail_staged_chmod)
    else:
        monkeypatch.setattr(
            spool_module.os,
            "link",
            lambda *_, **__: (_ for _ in ()).throw(OSError("publish")),
        )

    with pytest.raises(OSError):
        spool.enqueue(payload(event(1)))

    assert spool.pending() == []
    assert not list(tmp_path.glob("*.json"))


def test_reject_uses_replace_and_preserves_committed_destination_after_fsync_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import monitor_agent.spool as spool_module

    spool = Spool(tmp_path, max_bytes=1_048_576, max_age_sec=3600)
    record = spool.enqueue(payload(event(1)))
    replace_calls: list[tuple[object, object]] = []
    original_replace = spool_module.os.replace

    def tracking_replace(
        source: object, destination: object, *args: object, **kwargs: object
    ) -> None:
        replace_calls.append((source, destination))
        original_replace(source, destination, *args, **kwargs)

    monkeypatch.setattr(spool_module.os, "replace", tracking_replace)
    monkeypatch.setattr(
        spool, "_fsync_directory", lambda _: (_ for _ in ()).throw(OSError("dir fsync"))
    )

    with pytest.raises(OSError, match="dir fsync"):
        spool.reject(record)

    assert replace_calls
    assert not record.exists()
    assert list((tmp_path / "dead-letter").glob("*.rejected.json"))


def test_path_operations_reject_hardlinked_records_without_touching_external_target(
    tmp_path: Path,
) -> None:
    spool = Spool(tmp_path / "spool", max_bytes=1_048_576, max_age_sec=3600)
    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps(payload(event(1))), encoding="utf-8")
    linked = spool.root / "linked.json"
    try:
        os.link(outside, linked)
    except OSError as error:
        pytest.skip(f"hard links unavailable: {error}")

    original_mode = stat.S_IMODE(outside.stat().st_mode)
    for operation in (spool.load, spool.ack, spool.reject):
        with pytest.raises(ValueError, match="not a pending spool record"):
            operation(linked)

    assert outside.exists()
    assert stat.S_IMODE(outside.stat().st_mode) == original_mode


def test_reject_collision_remains_idempotent_for_the_same_source_record(tmp_path: Path) -> None:
    spool = Spool(tmp_path, max_bytes=1_048_576, max_age_sec=3600)
    record = spool.enqueue(payload(event(1)))
    canonical = tmp_path / "dead-letter" / f"{record.stem}.rejected.json"
    canonical.write_text("unrelated", encoding="utf-8")

    first = spool.reject(record)
    second = spool.reject(record)

    assert first != canonical
    assert second == first
    assert first.name.endswith(".rejected.json")
    assert canonical.read_text(encoding="utf-8") == "unrelated"


def test_reject_disambiguates_an_existing_attributed_destination(tmp_path: Path) -> None:
    spool = Spool(tmp_path, max_bytes=1_048_576, max_age_sec=3600)
    record = spool.enqueue(payload(event(1)))
    attributed = tmp_path / "dead-letter" / spool._dead_letter_name(record, ".rejected")
    attributed.write_text("unrelated", encoding="utf-8")

    rejected = spool.reject(record)

    assert rejected != attributed
    assert spool.reject(record) == rejected
    assert json.loads(rejected.read_text(encoding="utf-8"))["event_id"] == event(1)


def test_directory_fsync_handles_unavailable_operations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import monitor_agent.spool as spool_module

    actual_open = spool_module.os.open
    monkeypatch.setattr(spool_module, "_supports_directory_fsync", lambda: True)

    def failing_open(*args: object) -> int:
        raise OSError("no dir")

    monkeypatch.setattr(spool_module.os, "open", failing_open)
    spool_module.Spool._fsync_directory(tmp_path)

    descriptor = actual_open(tmp_path, os.O_RDONLY)
    monkeypatch.setattr(spool_module.os, "open", lambda *args: descriptor)

    def unsupported_fsync(value: int) -> None:
        raise OSError(errno.EINVAL, "unsupported")

    monkeypatch.setattr(spool_module.os, "fsync", unsupported_fsync)
    spool_module.Spool._fsync_directory(tmp_path)
