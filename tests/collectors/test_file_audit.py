from __future__ import annotations

import hashlib
from contextlib import suppress
from pathlib import Path

import pytest

from monitor_agent.collectors import file_audit
from monitor_agent.collectors.file_audit import FileAuditCollector
from monitor_agent.models import CollectorStatus


def test_file_audit_is_disabled_without_explicit_paths() -> None:
    payload = FileAuditCollector((), 50, 10485760).collect()

    assert payload.status is CollectorStatus.DISABLED
    assert payload.data == {"file_audit": []}


def test_file_audit_records_bounded_file_metadata_and_digest(tmp_path: Path) -> None:
    first = tmp_path / "a.txt"
    second = tmp_path / "b.txt"
    third = tmp_path / "c.txt"
    first.write_bytes(b"alpha")
    second.write_bytes(b"beta")
    third.write_bytes(b"gamma")

    payload = FileAuditCollector((tmp_path,), 2, 1024).collect()

    assert payload.status is CollectorStatus.SUCCESS
    records = payload.data["file_audit"]
    assert [record["path"] for record in records] == [str(first), str(second)]
    assert records[0]["size_bytes"] == 5
    assert records[0]["sha256"] == hashlib.sha256(b"alpha").hexdigest()
    assert isinstance(records[0]["modified"], str)


def test_file_audit_skips_symlinks_and_reports_unavailable_inputs(
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"outside")
    root = tmp_path / "root"
    root.mkdir()
    with suppress(OSError):
        (root / "link.txt").symlink_to(outside)
    oversized = root / "oversized.bin"
    oversized.write_bytes(b"x" * 9)
    missing = tmp_path / "missing"

    payload = FileAuditCollector((root, missing), 10, 8).collect()

    assert payload.status is CollectorStatus.PARTIAL
    assert payload.error_code == "file_audit_partial"
    assert payload.error_message == "some audit targets were unavailable or exceeded limits"
    assert payload.data == {"file_audit": []}


def test_file_audit_reports_directory_walk_errors(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def fail_walk(
        target: Path,
        *,
        followlinks: bool,
        onerror: object,
    ) -> list[tuple[str, list[str], list[str]]]:
        assert target == tmp_path
        assert followlinks is False
        assert callable(onerror)
        onerror(PermissionError("denied"))
        return []

    monkeypatch.setattr(file_audit.os, "walk", fail_walk)

    payload = FileAuditCollector((tmp_path,), 10, 1024).collect()

    assert payload.status is CollectorStatus.PARTIAL
    assert payload.error_code == "file_audit_partial"
    assert payload.data == {"file_audit": []}
