from __future__ import annotations

import errno
import json
import os
import stat
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from threading import Condition, Event
from uuid import UUID

import pytest

from monitor_agent.models import RetentionResult, SpoolStats
from monitor_agent.spool import Spool


class FakeWindowsLocking:
    LK_LOCK = 1
    LK_NBLCK = 2
    LK_UNLCK = 3

    def __init__(self) -> None:
        self._condition = Condition()
        self._owners: dict[tuple[int, int], int] = {}
        self.blocking_attempted = Event()
        self.try_descriptors: list[int] = []
        self.unlocked_descriptors: list[int] = []

    @property
    def locked_count(self) -> int:
        with self._condition:
            return len(self._owners)

    def locking(self, descriptor: int, mode: int, length: int) -> None:
        assert length == 1
        assert os.fstat(descriptor).st_size >= 1
        assert os.lseek(descriptor, 0, os.SEEK_CUR) == 0
        record_stat = os.fstat(descriptor)
        identity = (record_stat.st_dev, record_stat.st_ino)
        with self._condition:
            if mode == self.LK_UNLCK:
                if self._owners.get(identity) == descriptor:
                    del self._owners[identity]
                self.unlocked_descriptors.append(descriptor)
                self._condition.notify_all()
                return
            if mode == self.LK_NBLCK:
                self.try_descriptors.append(descriptor)
                if identity in self._owners:
                    raise OSError(errno.EACCES, "locked")
            else:
                while identity in self._owners:
                    self.blocking_attempted.set()
                    self._condition.wait()
            self._owners[identity] = descriptor


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

    def fail_replace(
        source_directory: Path,
        source_name: str,
        destination_directory: Path,
        destination_name: str,
    ) -> None:
        raise OSError("publish failed")

    monkeypatch.setattr(spool, "_publish_noreplace", fail_replace)
    with pytest.raises(OSError, match="publish failed"):
        spool.enqueue(payload(event(1)))

    assert spool.pending() == []
    assert not list(tmp_path.glob("*.tmp"))
    assert not list(tmp_path.glob("*.json"))


def test_enqueue_cleanup_unlinks_temp_when_staged_descriptor_close_retry_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spool = Spool(tmp_path, max_bytes=1_048_576, max_age_sec=3600)
    original_close = spool._close_locked_descriptor
    staged_descriptor: int | None = None
    close_attempts = 0

    monkeypatch.setattr(
        spool,
        "_publish_staged",
        lambda *args: (_ for _ in ()).throw(OSError("publish failed")),
    )

    def fail_staged_close_twice(descriptor: int) -> None:
        nonlocal staged_descriptor, close_attempts
        if staged_descriptor is None:
            staged_descriptor = descriptor
            close_attempts += 1
            original_close(descriptor)
            raise OSError("staged close failed")
        if descriptor == staged_descriptor:
            close_attempts += 1
            raise OSError("staged close retry failed")
        original_close(descriptor)

    monkeypatch.setattr(spool, "_close_locked_descriptor", fail_staged_close_twice)

    with pytest.raises(OSError, match="publish failed"):
        spool.enqueue(payload(event(1)))

    assert close_attempts == 2
    assert not list(tmp_path.glob(".spool-*.tmp"))
    assert spool._artifact_guards == {}


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
    assert (
        len(
            [
                path
                for path in (tmp_path / "dead-letter").glob("*.json")
                if not path.name.startswith(".")
            ]
        )
        == 3
    )


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
        monkeypatch.setattr(
            spool_module.os,
            "fchmod",
            lambda *_, **__: (_ for _ in ()).throw(OSError("chmod")),
        )
    else:
        monkeypatch.setattr(
            spool,
            "_publish_noreplace",
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

    def fail_after_record_commit(directory: Path) -> None:
        if directory == spool.root and not record.exists():
            raise OSError("dir fsync")

    monkeypatch.setattr(spool, "_fsync_directory", fail_after_record_commit)

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


@pytest.mark.parametrize("operation", ["corrupt", "reject"])
def test_dead_letter_chmod_uses_a_validated_file_descriptor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    import monitor_agent.spool as spool_module

    spool = Spool(tmp_path, max_bytes=1_048_576, max_age_sec=3600)
    if operation == "corrupt":
        record = tmp_path / "20260720T120000000000Z_corrupt.json"
        record.write_text("{", encoding="utf-8")
    else:
        record = spool.enqueue(payload(event(1)))
    record.chmod(0o644)

    fchmod_calls: list[tuple[int, int]] = []
    original_chmod = spool_module.os.chmod
    original_fchmod = spool_module.os.fchmod

    def forbid_record_path_chmod(path: object, mode: int, *args: object, **kwargs: object) -> None:
        if str(path).endswith(".json"):
            raise AssertionError("record chmod must be descriptor based")
        original_chmod(path, mode, *args, **kwargs)

    def track_fchmod(descriptor: int, mode: int) -> None:
        fchmod_calls.append((descriptor, mode))
        original_fchmod(descriptor, mode)

    monkeypatch.setattr(spool_module.os, "chmod", forbid_record_path_chmod)
    monkeypatch.setattr(spool_module.os, "fchmod", track_fchmod)

    result = spool.load(record) if operation == "corrupt" else spool.reject(record)

    destination = next(
        path for path in (tmp_path / "dead-letter").glob("*.json") if not path.name.startswith(".")
    )
    assert result is None if operation == "corrupt" else result == destination
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600
    assert fchmod_calls


def test_enqueue_publishes_with_replace_without_hard_links(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if sys.platform == "darwin":
        pytest.skip("macOS uses the POSIX hard-link no-replace fallback")

    import monitor_agent.spool as spool_module

    spool = Spool(tmp_path, max_bytes=1_048_576, max_age_sec=3600)
    monkeypatch.setattr(
        spool_module.os,
        "link",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("hard link publish")),
    )

    record = spool.enqueue(payload(event(1)))

    assert record.exists()
    assert record.stat().st_nlink == 1
    assert spool.pending() == [record]


def test_enqueue_failure_after_reservation_leaves_no_visible_record_and_recovers_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spool = Spool(tmp_path, max_bytes=1_048_576, max_age_sec=3600)
    original_publish = spool._publish_noreplace

    def fail_pending_publish(
        source_directory: Path,
        source_name: str,
        destination_directory: Path,
        destination_name: str,
    ) -> None:
        if destination_directory == spool.root and destination_name.endswith(".json"):
            raise OSError("crash after reservation")
        original_publish(source_directory, source_name, destination_directory, destination_name)

    monkeypatch.setattr(spool, "_publish_noreplace", fail_pending_publish)

    with pytest.raises(OSError, match="crash after reservation"):
        spool.enqueue(payload(event(1)))

    assert not list(tmp_path.glob("*.json"))
    assert list(tmp_path.glob(".publish-*.lock"))
    Spool(tmp_path, max_bytes=1_048_576, max_age_sec=3600)
    assert not list(tmp_path.glob(".spool-*.tmp"))
    assert not list(tmp_path.glob(".publish-*.lock"))


def test_enqueue_committed_record_survives_reservation_cleanup_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spool = Spool(tmp_path, max_bytes=1_048_576, max_age_sec=3600)
    original_unlink = spool._unlink_name
    original_unlink_open = spool._unlink_open_name

    def leave_publish_reservation(directory: Path, name: str) -> None:
        if name.startswith(".publish-"):
            return
        original_unlink(directory, name)

    def fail_stale_publish_cleanup(directory: Path, name: str, descriptor: int) -> bool:
        if name.startswith(".publish-"):
            return False
        return original_unlink_open(directory, name, descriptor)

    monkeypatch.setattr(spool, "_unlink_name", leave_publish_reservation)
    monkeypatch.setattr(spool, "_unlink_open_name", fail_stale_publish_cleanup)

    record = spool.enqueue(payload(event(1)))

    assert record.exists()
    assert record.stat().st_nlink == 1
    assert list(tmp_path.glob(".publish-*.lock"))
    recovered = Spool(tmp_path, max_bytes=1_048_576, max_age_sec=3600)
    assert recovered.pending() == [record]
    assert not list(tmp_path.glob(".publish-*.lock"))


def test_dead_letter_record_moves_directly_to_committed_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spool = Spool(tmp_path, max_bytes=1_048_576, max_age_sec=3600)
    record = spool.enqueue(payload(event(1)))
    record_moves: list[tuple[str, str]] = []
    original_publish = spool._publish_noreplace

    def track_replace(
        source_directory: Path,
        source_name: str,
        destination_directory: Path,
        destination_name: str,
    ) -> None:
        if source_directory == spool.root and source_name == record.name:
            record_moves.append((source_name, destination_name))
        original_publish(source_directory, source_name, destination_directory, destination_name)

    monkeypatch.setattr(spool, "_publish_noreplace", track_replace)

    rejected = spool.reject(record)

    assert record_moves == [(record.name, rejected.name)]
    assert not list((tmp_path / "dead-letter").glob(".move-*"))
    assert rejected.exists()


def test_dead_letter_crash_before_direct_replace_replays_persisted_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spool = Spool(tmp_path, max_bytes=1_048_576, max_age_sec=3600)
    record = spool.enqueue(payload(event(1)))
    original_publish = spool._publish_noreplace

    def fail_record_move(
        source_directory: Path,
        source_name: str,
        destination_directory: Path,
        destination_name: str,
    ) -> None:
        if source_directory == spool.root and source_name == record.name:
            raise OSError("crash before direct replace")
        original_publish(source_directory, source_name, destination_directory, destination_name)

    monkeypatch.setattr(spool, "_publish_noreplace", fail_record_move)
    with pytest.raises(OSError, match="crash before direct replace"):
        spool.reject(record)

    markers = list((tmp_path / "dead-letter").glob(".deadop-*.json"))
    assert len(markers) == 1
    destination_name = json.loads(markers[0].read_text(encoding="utf-8"))["destination"]
    assert record.exists()
    assert not (tmp_path / "dead-letter" / destination_name).exists()

    monkeypatch.setattr(spool, "_publish_noreplace", original_publish)
    rejected = spool.reject(record)
    assert rejected.name == destination_name
    assert not record.exists()


def test_dead_letter_committed_record_survives_reservation_cleanup_and_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spool = Spool(tmp_path, max_bytes=1_048_576, max_age_sec=3600)
    record = spool.enqueue(payload(event(1)))
    original_unlink = spool._unlink_name

    def leave_dead_letter_reservations(directory: Path, name: str) -> None:
        if name.startswith((".deadres-", ".deadop-")) and name.endswith(".lock"):
            return
        original_unlink(directory, name)

    monkeypatch.setattr(spool, "_unlink_name", leave_dead_letter_reservations)
    rejected = spool.reject(record)

    assert rejected.exists()
    assert not record.exists()
    assert list((tmp_path / "dead-letter").glob(".deadres-*.lock"))
    assert not list((tmp_path / "dead-letter").glob(".move-*"))

    recovered = Spool(tmp_path, max_bytes=1_048_576, max_age_sec=3600)
    assert recovered.reject(record) == rejected
    assert recovered.stats().dead_letter_count == 1
    assert not list((tmp_path / "dead-letter").glob("*.lock"))


def test_reject_marker_is_authoritative_across_arbitrary_alternate_collisions(
    tmp_path: Path,
) -> None:
    spool = Spool(tmp_path, max_bytes=1_048_576, max_age_sec=3600)
    record = spool.enqueue(payload(event(1)))
    canonical = tmp_path / "dead-letter" / spool._dead_letter_name(record, ".rejected")
    stem, extension = canonical.name.rsplit(".", 1)
    for name in (
        canonical.name,
        f"{stem}.1.{extension}",
        f"{stem}.2.{extension}",
        f"{stem}.0000000000000000.{extension}",
    ):
        (canonical.parent / name).write_text(f"unrelated:{name}", encoding="utf-8")

    first = spool.reject(record)
    lexically_prior = canonical.parent / f"{stem}.00000000000000000.{extension}"
    lexically_prior.write_text("another unrelated record", encoding="utf-8")
    second = spool.reject(record)

    assert first.name not in {
        canonical.name,
        f"{stem}.1.{extension}",
        f"{stem}.2.{extension}",
        f"{stem}.0000000000000000.{extension}",
    }
    assert second == first
    assert json.loads(first.read_text(encoding="utf-8"))["event_id"] == event(1)


def test_reject_missing_validates_marker_destination_without_following_links(
    tmp_path: Path,
) -> None:
    spool = Spool(tmp_path / "spool", max_bytes=1_048_576, max_age_sec=3600)
    record = spool.enqueue(payload(event(1)))
    rejected = spool.reject(record)
    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps(payload(event(2))), encoding="utf-8")
    rejected.unlink()
    try:
        rejected.symlink_to(outside)
    except OSError as error:
        pytest.skip(f"symlinks unavailable: {error}")

    with pytest.raises((OSError, ValueError)):
        spool.reject(record)

    assert outside.exists()


def test_cross_instance_publish_reservation_chooses_deterministic_next_candidate(
    tmp_path: Path,
) -> None:
    first = Spool(tmp_path, max_bytes=1_048_576, max_age_sec=3600)
    second = Spool(tmp_path, max_bytes=1_048_576, max_age_sec=3600)
    now = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)
    canonical_name = first._record_name(payload(event(1)), now)
    reservation_name = first._publish_reservation_name(canonical_name)
    reservation = first._acquire_reservation(first.root, reservation_name)
    assert reservation is not None
    executor = ThreadPoolExecutor(max_workers=1)
    try:
        record = executor.submit(second.enqueue, payload(event(1)), now=now).result(timeout=2)
    finally:
        first._release_reservation(first.root, reservation_name, reservation)
        executor.shutdown(wait=True)

    assert record.name == first._numbered_name(canonical_name, 1)
    assert record.stat().st_nlink == 1


@pytest.mark.parametrize(
    ("now", "expected_suffix", "canonical_and_first_share_guard"),
    [
        pytest.param(
            datetime(2026, 7, 20, 12, 0, tzinfo=UTC),
            1,
            False,
            id="canonical-and-first-candidate-use-distinct-guards",
        ),
        pytest.param(
            datetime(2026, 7, 20, 12, 0, 0, 200, tzinfo=UTC),
            2,
            True,
            id="canonical-and-first-candidate-share-a-guard",
        ),
    ],
)
def test_cross_instance_dead_letter_reservation_chooses_next_destination(
    tmp_path: Path,
    now: datetime,
    expected_suffix: int,
    canonical_and_first_share_guard: bool,
) -> None:
    first = Spool(tmp_path, max_bytes=1_048_576, max_age_sec=3600)
    second = Spool(tmp_path, max_bytes=1_048_576, max_age_sec=3600)
    record = first.enqueue(payload(event(1)), now=now)
    canonical_name = first._dead_letter_name(record, ".rejected")
    reservation_name = first._dead_letter_reservation_name(canonical_name)
    first_candidate_name = first._numbered_name(canonical_name, 1)
    canonical_guard = first._artifact_guard_name(reservation_name)
    first_candidate_guard = first._artifact_guard_name(
        first._dead_letter_reservation_name(first_candidate_name)
    )
    assert (canonical_guard == first_candidate_guard) is canonical_and_first_share_guard
    if canonical_and_first_share_guard:
        second_candidate_name = first._numbered_name(canonical_name, 2)
        second_candidate_guard = first._artifact_guard_name(
            first._dead_letter_reservation_name(second_candidate_name)
        )
        assert canonical_guard != second_candidate_guard

    reservation = first._acquire_reservation(first._dead_letter, reservation_name)
    assert reservation is not None
    try:
        rejected = second.reject(record)
    finally:
        first._release_reservation(first._dead_letter, reservation_name, reservation)

    assert rejected.name == first._numbered_name(canonical_name, expected_suffix)
    assert rejected.exists()


@pytest.mark.parametrize(
    "marker",
    [
        "{",
        "[]",
        '{"destination":"safe.json","source":"wrong.json","suffix":".rejected"}',
        '{"destination":"../escape.json","source":"SOURCE","suffix":".rejected"}',
    ],
)
def test_dead_letter_operation_marker_rejects_tampering(tmp_path: Path, marker: str) -> None:
    spool = Spool(tmp_path, max_bytes=1_048_576, max_age_sec=3600)
    source = tmp_path / "20260720T120000000000Z_missing.json"
    operation_id = spool._operation_id(source.name, ".rejected")
    marker_path = tmp_path / "dead-letter" / f".deadop-{operation_id}.json"
    marker_path.write_text(marker.replace("SOURCE", source.name), encoding="utf-8")

    with pytest.raises(ValueError, match="dead-letter operation"):
        spool.reject(source)


def test_hidden_artifact_cleanup_preserves_links_and_active_reservations(
    tmp_path: Path,
) -> None:
    spool = Spool(tmp_path / "spool", max_bytes=1_048_576, max_age_sec=3600)
    outside = tmp_path / "outside"
    outside.write_text("outside", encoding="utf-8")
    hardlink = spool.root / ".spool-hardlink.tmp"
    symlink = spool.root / ".publish-symlink.lock"
    try:
        os.link(outside, hardlink)
        symlink.symlink_to(outside)
    except OSError as error:
        pytest.skip(f"links unavailable: {error}")
    active_name = ".publish-active.lock"
    active = spool._acquire_reservation(spool.root, active_name)
    assert active is not None

    try:
        spool.pending()
        assert hardlink.exists()
        assert symlink.is_symlink()
        assert (spool.root / active_name).exists()
    finally:
        spool._release_reservation(spool.root, active_name, active)

    assert outside.read_text(encoding="utf-8") == "outside"


def test_stale_reservation_recovery_and_failed_lock_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spool = Spool(tmp_path, max_bytes=1_048_576, max_age_sec=3600)
    stale_name = ".publish-stale.lock"
    (tmp_path / stale_name).write_bytes(b"")
    recovered = spool._acquire_reservation(spool.root, stale_name)
    assert recovered is not None
    spool._release_reservation(spool.root, stale_name, recovered)

    failed_name = ".publish-failed.lock"
    monkeypatch.setattr(
        spool,
        "_lock_descriptor",
        lambda _: (_ for _ in ()).throw(OSError("lock failed")),
    )
    with pytest.raises(OSError, match="lock failed"):
        spool._acquire_reservation(spool.root, failed_name)
    assert not (tmp_path / failed_name).exists()
    spool._release_reservation(spool.root, failed_name, None)


def test_descriptor_relative_identity_helpers_detect_replacement(tmp_path: Path) -> None:
    spool = Spool(tmp_path, max_bytes=1_048_576, max_age_sec=3600)
    hidden = tmp_path / ".spool-replaced.tmp"
    hidden.write_text("first", encoding="utf-8")
    descriptor = os.open(hidden, os.O_RDONLY)
    hidden.unlink()
    hidden.write_text("second", encoding="utf-8")
    try:
        assert not spool._open_name_matches(spool.root, hidden.name, descriptor)
        assert not spool._unlink_open_name(spool.root, hidden.name, descriptor)
    finally:
        os.close(descriptor)
    assert hidden.read_text(encoding="utf-8") == "second"


def test_directory_fsync_reraises_real_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import monitor_agent.spool as spool_module

    monkeypatch.setattr(spool_module, "_supports_directory_fsync", lambda: True)
    monkeypatch.setattr(
        spool_module.os,
        "fsync",
        lambda _: (_ for _ in ()).throw(OSError(errno.EIO, "I/O error")),
    )

    with pytest.raises(OSError, match="I/O error"):
        spool_module.Spool._fsync_directory(tmp_path)


def test_active_dead_letter_operation_lock_keeps_source_replay_visible(
    tmp_path: Path,
) -> None:
    spool = Spool(tmp_path, max_bytes=1_048_576, max_age_sec=3600)
    record = spool.enqueue(payload(event(1)))
    operation_id = spool._operation_id(record.name, ".rejected")
    lock_name = f".deadop-{operation_id}.lock"
    operation_lock = spool._acquire_reservation(spool._dead_letter, lock_name)
    assert operation_lock is not None
    try:
        with pytest.raises(OSError, match="already in progress"):
            spool.reject(record)
    finally:
        spool._release_reservation(spool._dead_letter, lock_name, operation_lock)

    assert record.exists()


def test_replayed_dead_letter_marker_respects_active_destination_reservation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spool = Spool(tmp_path, max_bytes=1_048_576, max_age_sec=3600)
    record = spool.enqueue(payload(event(1)))
    original_publish = spool._publish_noreplace

    def fail_record_move(
        source_directory: Path,
        source_name: str,
        destination_directory: Path,
        destination_name: str,
    ) -> None:
        if source_directory == spool.root and source_name == record.name:
            raise OSError("crash")
        original_publish(source_directory, source_name, destination_directory, destination_name)

    monkeypatch.setattr(spool, "_publish_noreplace", fail_record_move)
    with pytest.raises(OSError, match="crash"):
        spool.reject(record)
    monkeypatch.setattr(spool, "_publish_noreplace", original_publish)

    marker = next((tmp_path / "dead-letter").glob(".deadop-*.json"))
    destination_name = json.loads(marker.read_text(encoding="utf-8"))["destination"]
    reservation_name = spool._dead_letter_reservation_name(destination_name)
    reservation = spool._acquire_reservation(spool._dead_letter, reservation_name)
    assert reservation is not None
    try:
        with pytest.raises(OSError, match="destination is reserved"):
            spool.reject(record)
    finally:
        spool._release_reservation(spool._dead_letter, reservation_name, reservation)

    assert spool.reject(record).name == destination_name


def test_dead_letter_detects_source_identity_change_before_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spool = Spool(tmp_path, max_bytes=1_048_576, max_age_sec=3600)
    record = spool.enqueue(payload(event(1)))
    monkeypatch.setattr(spool, "_open_name_matches", lambda *args: False)

    with pytest.raises(ValueError, match="source changed before commit"):
        spool.reject(record)

    assert record.exists()
    assert not [
        path for path in (tmp_path / "dead-letter").glob("*.json") if not path.name.startswith(".")
    ]


def test_dead_letter_retry_with_recreated_source_returns_marker_destination(
    tmp_path: Path,
) -> None:
    spool = Spool(tmp_path, max_bytes=1_048_576, max_age_sec=3600)
    record = spool.enqueue(payload(event(1)))
    contents = record.read_bytes()
    rejected = spool.reject(record)
    record.write_bytes(contents)
    record.chmod(0o600)

    assert spool.reject(record) == rejected
    assert record.exists()
    assert rejected.exists()


def test_descriptor_helpers_reject_missing_and_hardlinked_names(tmp_path: Path) -> None:
    spool = Spool(tmp_path / "spool", max_bytes=1_048_576, max_age_sec=3600)
    missing = spool.root / "missing.json"
    assert not spool._is_regular_file(missing)
    with pytest.raises(ValueError, match="not a pending spool record"):
        spool._validated_record_stat(missing)

    outside = tmp_path / "outside.json"
    outside.write_text("{}", encoding="utf-8")
    hardlink = spool.root / "hardlink.json"
    try:
        os.link(outside, hardlink)
    except OSError as error:
        pytest.skip(f"hard links unavailable: {error}")
    with pytest.raises(ValueError, match="not a pending spool record"):
        spool._open_record(spool.root, hardlink.name)


def test_windows_locking_preserves_live_hidden_artifacts_and_recovers_stale_ones(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import monitor_agent.spool as spool_module

    class FakeMsvcrt:
        LK_LOCK = 1
        LK_NBLCK = 2
        LK_UNLCK = 3

        def __init__(self) -> None:
            self.locked_inodes: set[tuple[int, int]] = set()
            self.try_descriptors: list[int] = []
            self.unlocked_descriptors: list[int] = []

        def locking(self, descriptor: int, mode: int, length: int) -> None:
            assert length == 1
            assert os.fstat(descriptor).st_size >= 1
            assert os.lseek(descriptor, 0, os.SEEK_CUR) == 0
            record_stat = os.fstat(descriptor)
            identity = (record_stat.st_dev, record_stat.st_ino)
            if mode == self.LK_UNLCK:
                self.locked_inodes.discard(identity)
                self.unlocked_descriptors.append(descriptor)
                return
            if mode == self.LK_NBLCK:
                self.try_descriptors.append(descriptor)
                if identity in self.locked_inodes:
                    raise OSError(errno.EACCES, "locked")
            self.locked_inodes.add(identity)

    spool = Spool(tmp_path, max_bytes=1_048_576, max_age_sec=3600)
    live = tmp_path / ".publish-live.lock"
    stale = tmp_path / ".publish-stale.lock"
    live.write_bytes(b"")
    stale.write_bytes(b"")
    owner_descriptor = os.open(live, os.O_RDWR)
    fake_msvcrt = FakeMsvcrt()
    monkeypatch.setattr(spool_module, "_platform_name", lambda: "nt")
    monkeypatch.setattr(spool_module, "_windows_locking", lambda: fake_msvcrt)

    spool._lock_descriptor(owner_descriptor)
    try:
        spool.pending()
    finally:
        spool._unlock_descriptor(owner_descriptor)
        os.close(owner_descriptor)

    assert live.exists()
    assert not stale.exists()
    assert fake_msvcrt.try_descriptors
    assert fake_msvcrt.unlocked_descriptors
    for descriptor in fake_msvcrt.try_descriptors:
        with pytest.raises(OSError, match="Bad file descriptor"):
            os.fstat(descriptor)


def test_pending_publish_never_overwrites_a_destination_created_after_reservation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spool = Spool(tmp_path, max_bytes=1_048_576, max_age_sec=3600)
    now = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)
    canonical = tmp_path / spool._record_name(payload(event(1)), now)
    original_publish = spool._publish_noreplace
    injected = False

    def race_publish(
        source_directory: Path,
        source_name: str,
        destination_directory: Path,
        destination_name: str,
    ) -> None:
        nonlocal injected
        if not injected and destination_name == canonical.name:
            injected = True
            canonical.write_bytes(b"unrelated")
        original_publish(source_directory, source_name, destination_directory, destination_name)

    monkeypatch.setattr(spool, "_publish_noreplace", race_publish)

    published = spool.enqueue(payload(event(1)), now=now)

    assert canonical.read_bytes() == b"unrelated"
    assert published.name == spool._numbered_name(canonical.name, 1)
    assert json.loads(published.read_text(encoding="utf-8"))["event_id"] == event(1)
    assert not list(tmp_path.glob(".spool-*.tmp"))


def test_dead_letter_publish_reselects_after_an_atomic_collision_and_retries_exactly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spool = Spool(tmp_path, max_bytes=1_048_576, max_age_sec=3600)
    record = spool.enqueue(payload(event(1)))
    original_publish = spool._publish_noreplace
    injected_destination: Path | None = None

    def race_publish(
        source_directory: Path,
        source_name: str,
        destination_directory: Path,
        destination_name: str,
    ) -> None:
        nonlocal injected_destination
        if source_directory == spool.root and injected_destination is None:
            injected_destination = destination_directory / destination_name
            injected_destination.write_bytes(b"unrelated")
        original_publish(source_directory, source_name, destination_directory, destination_name)

    monkeypatch.setattr(spool, "_publish_noreplace", race_publish)

    rejected = spool.reject(record)

    assert injected_destination is not None
    assert injected_destination.read_bytes() == b"unrelated"
    assert rejected != injected_destination
    assert json.loads(rejected.read_text(encoding="utf-8"))["event_id"] == event(1)
    assert spool.reject(record) == rejected


def test_dead_letter_never_accepts_an_identical_uncommitted_collision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spool = Spool(tmp_path, max_bytes=1_048_576, max_age_sec=3600)
    record = spool.enqueue(payload(event(1)))
    source_bytes = record.read_bytes()
    original_publish = spool._publish_noreplace
    injected_destination: Path | None = None

    def race_publish(
        source_directory: Path,
        source_name: str,
        destination_directory: Path,
        destination_name: str,
    ) -> None:
        nonlocal injected_destination
        if source_directory == spool.root and injected_destination is None:
            injected_destination = destination_directory / destination_name
            injected_destination.write_bytes((source_directory / source_name).read_bytes())
        original_publish(source_directory, source_name, destination_directory, destination_name)

    monkeypatch.setattr(spool, "_publish_noreplace", race_publish)

    rejected = spool.reject(record)

    assert injected_destination is not None
    assert rejected != injected_destination
    assert injected_destination.read_bytes() == source_bytes
    assert rejected.read_bytes() == source_bytes
    assert not record.exists()
    assert spool.reject(record) == rejected


def test_posix_link_fallback_recovers_a_pending_publish_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spool = Spool(tmp_path, max_bytes=1_048_576, max_age_sec=3600)
    staged = tmp_path / ".spool-fallback.tmp"
    destination = tmp_path / "20260720T120000000000Z_fallback.json"
    staged.write_text(json.dumps(payload(event(1))), encoding="utf-8")
    original_unlink = os.unlink

    monkeypatch.setattr(spool, "_linux_rename_noreplace", lambda *args: False)

    def crash_after_link(path: object, *args: object, **kwargs: object) -> None:
        if path == staged.name:
            raise OSError("crash after link")
        original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(os, "unlink", crash_after_link)
    with pytest.raises(OSError, match="crash after link"):
        spool._publish_noreplace(spool.root, staged.name, spool.root, destination.name)

    assert staged.stat().st_ino == destination.stat().st_ino
    assert destination.stat().st_nlink == 2

    monkeypatch.setattr(os, "unlink", original_unlink)
    recovered = Spool(tmp_path, max_bytes=1_048_576, max_age_sec=3600)

    assert not staged.exists()
    assert recovered.pending() == [destination]
    assert destination.stat().st_nlink == 1


def test_posix_link_fallback_recovers_a_dead_letter_publish_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spool = Spool(tmp_path, max_bytes=1_048_576, max_age_sec=3600)
    record = spool.enqueue(payload(event(1)))
    original_unlink = os.unlink

    monkeypatch.setattr(spool, "_linux_rename_noreplace", lambda *args: False)

    def crash_after_link(path: object, *args: object, **kwargs: object) -> None:
        if path == record.name:
            raise OSError("crash after link")
        original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(os, "unlink", crash_after_link)
    with pytest.raises(OSError, match="crash after link"):
        spool.reject(record)

    marker = next((tmp_path / "dead-letter").glob(".deadop-*.json"))
    destination = (
        tmp_path / "dead-letter" / json.loads(marker.read_text(encoding="utf-8"))["destination"]
    )
    assert record.stat().st_ino == destination.stat().st_ino
    assert destination.stat().st_nlink == 2

    monkeypatch.setattr(os, "unlink", original_unlink)
    rejected = spool.reject(record)

    assert rejected == destination
    assert not record.exists()
    assert destination.stat().st_nlink == 1


@pytest.mark.parametrize(
    ("error_number", "expected"),
    [
        (errno.EEXIST, FileExistsError),
        (errno.ENOSYS, False),
        (errno.EIO, OSError),
    ],
)
def test_linux_rename_noreplace_maps_kernel_errors_exactly(
    monkeypatch: pytest.MonkeyPatch,
    error_number: int,
    expected: type[OSError] | bool,
) -> None:
    import monitor_agent.spool as spool_module

    class FakeRename:
        def __init__(self) -> None:
            self.argtypes: list[object] = []
            self.restype: object = None

        def __call__(self, *args: object) -> int:
            return -1

    class FakeLibc:
        def __init__(self) -> None:
            self.renameat2 = FakeRename()

    monkeypatch.setattr(spool_module, "CDLL", lambda *args, **kwargs: FakeLibc())
    monkeypatch.setattr(spool_module, "get_errno", lambda: error_number)

    if expected is False:
        assert not Spool._linux_rename_noreplace(1, "source", 2, "destination")
    else:
        with pytest.raises(expected) as caught:
            Spool._linux_rename_noreplace(1, "source", 2, "destination")
        assert caught.value.errno == error_number


def test_linux_rename_noreplace_falls_back_when_libc_has_no_symbol(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import monitor_agent.spool as spool_module

    monkeypatch.setattr(spool_module, "CDLL", lambda *args, **kwargs: object())

    assert not Spool._linux_rename_noreplace(1, "source", 2, "destination")


def test_dead_letter_marker_rejects_changed_source_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spool = Spool(tmp_path, max_bytes=1_048_576, max_age_sec=3600)
    record = spool.enqueue(payload(event(1)))
    original_publish = spool._publish_noreplace

    def fail_record_move(
        source_directory: Path,
        source_name: str,
        destination_directory: Path,
        destination_name: str,
    ) -> None:
        if source_directory == spool.root:
            raise OSError("crash")
        original_publish(source_directory, source_name, destination_directory, destination_name)

    monkeypatch.setattr(spool, "_publish_noreplace", fail_record_move)
    with pytest.raises(OSError, match="crash"):
        spool.reject(record)

    record.write_text(json.dumps(payload(event(2))), encoding="utf-8")
    monkeypatch.setattr(spool, "_publish_noreplace", original_publish)

    with pytest.raises(ValueError, match="does not match source content"):
        spool.reject(record)


def test_missing_rejection_rejects_a_replaced_destination_with_wrong_content(
    tmp_path: Path,
) -> None:
    spool = Spool(tmp_path, max_bytes=1_048_576, max_age_sec=3600)
    record = spool.enqueue(payload(event(1)))
    rejected = spool.reject(record)
    rejected.write_text(json.dumps(payload(event(2))), encoding="utf-8")

    with pytest.raises(ValueError, match="does not match operation"):
        spool.reject(record)


def test_windows_reclaims_an_unlocked_staging_file_with_its_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import monitor_agent.spool as spool_module

    spool = Spool(tmp_path, max_bytes=1_048_576, max_age_sec=3600)
    staged = tmp_path / ".spool-ambiguous.tmp"
    staged.write_bytes(b"complete but ownership is unknown")
    fake_msvcrt = FakeWindowsLocking()
    monkeypatch.setattr(spool_module, "_platform_name", lambda: "nt")
    monkeypatch.setattr(spool_module, "_windows_locking", lambda: fake_msvcrt)

    assert spool._cleanup_stale_hidden_file(spool.root, staged.name)
    assert not staged.exists()
    assert fake_msvcrt.locked_count == 0


def test_windows_publish_uses_a_rename_that_propagates_destination_collisions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import monitor_agent.spool as spool_module

    spool = Spool(tmp_path, max_bytes=1_048_576, max_age_sec=3600)
    source = tmp_path / ".spool-source.tmp"
    destination = tmp_path / "destination.json"
    source.write_bytes(b"source")
    destination.write_bytes(b"existing")
    monkeypatch.setattr(spool_module, "_platform_name", lambda: "nt")

    def collide(source_path: Path, destination_path: Path) -> None:
        assert source_path == source
        assert destination_path == destination
        raise FileExistsError(errno.EEXIST, "exists", destination_path)

    monkeypatch.setattr(os, "rename", collide)

    with pytest.raises(FileExistsError):
        spool._publish_noreplace(spool.root, source.name, spool.root, destination.name)

    assert source.read_bytes() == b"source"
    assert destination.read_bytes() == b"existing"


def test_missing_rejection_returns_its_persisted_unpublished_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spool = Spool(tmp_path, max_bytes=1_048_576, max_age_sec=3600)
    record = spool.enqueue(payload(event(1)))

    monkeypatch.setattr(
        spool,
        "_publish_noreplace",
        lambda *args: (_ for _ in ()).throw(OSError("crash before publish")),
    )
    with pytest.raises(OSError, match="crash before publish"):
        spool.reject(record)
    record.unlink()

    planned = spool.reject(record)

    assert planned.parent == tmp_path / "dead-letter"
    assert not planned.exists()


def test_posix_fallback_recovery_waits_for_the_publisher_to_unlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    publisher = Spool(tmp_path, max_bytes=1_048_576, max_age_sec=3600)
    observer = Spool(tmp_path, max_bytes=1_048_576, max_age_sec=3600)
    link_created = Event()
    finish_unlink = Event()
    original_unlink = os.unlink
    publish_errors: list[BaseException] = []
    published: list[Path] = []

    monkeypatch.setattr(publisher, "_linux_rename_noreplace", lambda *args: False)

    def pause_after_link(path: object, *args: object, **kwargs: object) -> None:
        if isinstance(path, str) and path.startswith(".spool-") and not link_created.is_set():
            link_created.set()
            if not finish_unlink.wait(timeout=5):
                raise TimeoutError("publisher unlink was not released")
        original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(os, "unlink", pause_after_link)

    def publish() -> None:
        try:
            published.append(publisher.enqueue(payload(event(1))))
        except BaseException as error:
            publish_errors.append(error)

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(publish)
        assert link_created.wait(timeout=5)
        hidden = next(tmp_path.glob(".spool-*.tmp"))
        destination = next(tmp_path.glob("*.json"))
        try:
            observer.pending()
            active_hidden_survived = hidden.exists()
        finally:
            finish_unlink.set()
        future.result(timeout=5)

    assert active_hidden_survived
    assert publish_errors == []
    assert published == [destination]
    assert not hidden.exists()
    assert destination.stat().st_nlink == 1


def test_posix_fallback_recovery_skips_a_locked_pair_then_reclaims_it(
    tmp_path: Path,
) -> None:
    owner = Spool(tmp_path, max_bytes=1_048_576, max_age_sec=3600)
    observer = Spool(tmp_path, max_bytes=1_048_576, max_age_sec=3600)
    staged = tmp_path / ".spool-locked.tmp"
    destination = tmp_path / "20260720T120000000000Z_locked.json"
    staged.write_text(json.dumps(payload(event(1))), encoding="utf-8")
    os.link(staged, destination)
    descriptor = os.open(staged, os.O_RDONLY)
    owner._lock_descriptor(descriptor)

    try:
        assert observer.pending() == []
        assert staged.exists()
        assert destination.stat().st_nlink == 2
    finally:
        owner._close_locked_descriptor(descriptor)

    assert observer.pending() == [destination]
    assert not staged.exists()
    assert destination.stat().st_nlink == 1


def test_windows_cleanup_cannot_unlink_a_new_owner_in_the_old_close_gap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import monitor_agent.spool as spool_module

    cleaner = Spool(tmp_path, max_bytes=1_048_576, max_age_sec=3600)
    owner = Spool(tmp_path, max_bytes=1_048_576, max_age_sec=3600)
    reservation_name = ".publish-handoff.lock"
    reservation_path = tmp_path / reservation_name
    reservation_path.write_bytes(b"")
    stale_identity = (reservation_path.stat().st_dev, reservation_path.stat().st_ino)
    fake_msvcrt = FakeWindowsLocking()
    closed_stale_descriptor = Event()
    let_cleaner_continue = Event()
    owner_attempted = Event()
    cleanup_finished = Event()
    owner_descriptor: list[int | None] = []
    original_close = cleaner._close_locked_descriptor

    monkeypatch.setattr(spool_module, "_platform_name", lambda: "nt")
    monkeypatch.setattr(spool_module, "_windows_locking", lambda: fake_msvcrt)

    def pause_after_stale_close(descriptor: int) -> None:
        descriptor_stat = os.fstat(descriptor)
        original_close(descriptor)
        if (descriptor_stat.st_dev, descriptor_stat.st_ino) == stale_identity:
            closed_stale_descriptor.set()
            if not let_cleaner_continue.wait(timeout=5):
                raise TimeoutError("cleanup gap was not released")

    monkeypatch.setattr(cleaner, "_close_locked_descriptor", pause_after_stale_close)

    def acquire_replacement() -> None:
        descriptor = owner._acquire_reservation(owner.root, reservation_name)
        owner_attempted.set()
        if descriptor is None:
            assert cleanup_finished.wait(timeout=5)
            descriptor = owner._acquire_reservation(owner.root, reservation_name)
        owner_descriptor.append(descriptor)

    with ThreadPoolExecutor(max_workers=2) as executor:
        cleanup_future = executor.submit(
            cleaner._cleanup_stale_hidden_file, cleaner.root, reservation_name
        )
        assert closed_stale_descriptor.wait(timeout=5)
        owner_future = executor.submit(acquire_replacement)
        assert owner_attempted.wait(timeout=5)
        let_cleaner_continue.set()
        assert cleanup_future.result(timeout=5)
        cleanup_finished.set()
        owner_future.result(timeout=5)

    assert owner_descriptor and owner_descriptor[0] is not None
    assert reservation_path.exists()
    owner._release_reservation(owner.root, reservation_name, owner_descriptor[0])
    assert not reservation_path.exists()
    assert fake_msvcrt.locked_count == 0


def test_windows_guards_preserve_active_stage_and_reclaim_all_stale_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import monitor_agent.spool as spool_module

    publisher = Spool(tmp_path, max_bytes=1_048_576, max_age_sec=3600)
    observer = Spool(tmp_path, max_bytes=1_048_576, max_age_sec=3600)
    fake_msvcrt = FakeWindowsLocking()
    stage_ready = Event()
    finish_publish = Event()
    original_publish = publisher._publish_staged

    monkeypatch.setattr(spool_module, "_platform_name", lambda: "nt")
    monkeypatch.setattr(spool_module, "_windows_locking", lambda: fake_msvcrt)

    def pause_with_active_stage(temporary: Path, name: str) -> Path:
        stage_ready.set()
        if not finish_publish.wait(timeout=5):
            raise TimeoutError("active stage was not released")
        return original_publish(temporary, name)

    monkeypatch.setattr(publisher, "_publish_staged", pause_with_active_stage)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(publisher.enqueue, payload(event(1)))
        assert stage_ready.wait(timeout=5)
        active_stage = next(tmp_path.glob(".spool-*.tmp"))
        observer.pending()
        assert active_stage.exists()
        finish_publish.set()
        published = future.result(timeout=5)

    assert published.exists()
    assert not list(tmp_path.glob(".spool-*.tmp"))

    hidden_artifacts = (
        (tmp_path, ".spool-crash.tmp"),
        (tmp_path, ".publish-crash.lock"),
        (tmp_path / "dead-letter", ".deadmark-crash.tmp"),
        (tmp_path / "dead-letter", ".deadop-crash.lock"),
        (tmp_path / "dead-letter", ".deadres-crash.lock"),
    )
    for crash_number in range(5):
        for directory, name in hidden_artifacts:
            (directory / f"{name}.{crash_number}").write_bytes(b"complete")
        observer.pending()

    for directory, name in hidden_artifacts:
        assert not list(directory.glob(f"{name}.*"))
    assert fake_msvcrt.locked_count == 0


def test_windows_stage_close_failure_cleans_temp_and_releases_its_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import monitor_agent.spool as spool_module

    spool = Spool(tmp_path, max_bytes=1_048_576, max_age_sec=3600)
    fake_msvcrt = FakeWindowsLocking()
    original_close = spool._close_locked_descriptor
    close_attempts = 0

    monkeypatch.setattr(spool_module, "_platform_name", lambda: "nt")
    monkeypatch.setattr(spool_module, "_windows_locking", lambda: fake_msvcrt)

    def fail_first_stage_close(descriptor: int) -> None:
        nonlocal close_attempts
        close_attempts += 1
        if close_attempts == 1:
            raise OSError("stage close failed")
        original_close(descriptor)

    monkeypatch.setattr(spool, "_close_locked_descriptor", fail_first_stage_close)

    with pytest.raises(OSError, match="stage close failed"):
        spool.enqueue(payload(event(1)))

    assert not list(tmp_path.glob(".spool-*.tmp"))
    assert spool._artifact_guards == {}
    assert fake_msvcrt.locked_count == 0


def test_windows_publish_close_failure_preserves_publish_error_and_releases_guards(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import monitor_agent.spool as spool_module

    spool = Spool(tmp_path, max_bytes=1_048_576, max_age_sec=3600)
    fake_msvcrt = FakeWindowsLocking()
    original_close = spool._close_locked_descriptor
    close_attempts = 0

    monkeypatch.setattr(spool_module, "_platform_name", lambda: "nt")
    monkeypatch.setattr(spool_module, "_windows_locking", lambda: fake_msvcrt)
    monkeypatch.setattr(
        spool,
        "_publish_noreplace",
        lambda *args: (_ for _ in ()).throw(OSError("publish failed")),
    )

    def fail_reservation_close(descriptor: int) -> None:
        nonlocal close_attempts
        close_attempts += 1
        if close_attempts == 2:
            raise OSError("reservation close failed")
        original_close(descriptor)

    monkeypatch.setattr(spool, "_close_locked_descriptor", fail_reservation_close)

    with pytest.raises(OSError, match="publish failed"):
        spool.enqueue(payload(event(1)))

    assert not list(tmp_path.glob(".spool-*.tmp"))
    assert list(tmp_path.glob(".publish-*.lock"))
    assert spool._artifact_guards == {}
    assert fake_msvcrt.locked_count == 0


def test_artifact_guard_slots_are_bounded_and_reject_unknown_names() -> None:
    assert Spool._artifact_guard_name(".spool-stage.tmp").endswith("spool")
    assert Spool._artifact_guard_name(".deadmark-stage.tmp").endswith("deadmark")
    for prefix, kind in (
        (".publish-", "publish"),
        (".deadop-", "deadop"),
        (".deadres-", "deadres"),
    ):
        guard_name = Spool._artifact_guard_name(f"{prefix}operation.lock")
        assert guard_name.startswith(f".artifact-guard-{kind}-")
        assert len(guard_name.rsplit("-", 1)[1]) == 2

    with pytest.raises(ValueError, match="unsupported hidden spool artifact"):
        Spool._artifact_guard_name(".unknown-artifact")


def test_artifact_guard_repairs_mode_and_rejects_hardlinked_guard(
    tmp_path: Path,
) -> None:
    spool = Spool(tmp_path, max_bytes=1_048_576, max_age_sec=3600)
    artifact_name = ".publish-mode.lock"
    guard = tmp_path / spool._artifact_guard_name(artifact_name)
    guard.write_bytes(b"")
    guard.chmod(0o640)

    assert spool._acquire_artifact_guard(spool.root, artifact_name, blocking=False)
    try:
        assert stat.S_IMODE(guard.stat().st_mode) == 0o600
    finally:
        spool._release_artifact_guard(spool.root, artifact_name)

    outside = tmp_path / "outside-guard"
    outside.write_bytes(b"")
    hardlinked_artifact = ".publish-hardlinked.lock"
    hardlinked_guard = tmp_path / spool._artifact_guard_name(hardlinked_artifact)
    os.link(outside, hardlinked_guard)
    with pytest.raises(ValueError, match="guard must be a regular file"):
        spool._acquire_artifact_guard(spool.root, hardlinked_artifact, blocking=False)


def test_artifact_guard_rejects_identity_change_after_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spool = Spool(tmp_path, max_bytes=1_048_576, max_age_sec=3600)
    match_results = iter((True, False))
    monkeypatch.setattr(
        Spool,
        "_open_name_matches",
        staticmethod(lambda *args: next(match_results)),
    )

    with pytest.raises(ValueError, match="guard changed while locking"):
        spool._acquire_artifact_guard(spool.root, ".publish-replaced.lock", blocking=True)
