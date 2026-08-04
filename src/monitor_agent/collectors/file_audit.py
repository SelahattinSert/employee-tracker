from __future__ import annotations

import hashlib
import os
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime
from pathlib import Path

from monitor_agent.models import CollectorPayload, CollectorStatus, JSONValue

_READ_CHUNK_BYTES = 65536
_PARTIAL_ERROR_CODE = "file_audit_partial"
_PARTIAL_ERROR_MESSAGE = "some audit targets were unavailable or exceeded limits"


def _raise_walk_error(error: OSError) -> None:
    raise error


def _iter_regular_files(target: Path) -> Iterator[Path]:
    if target.is_symlink():
        return
    if target.is_file():
        yield target
        return
    if not target.is_dir():
        return

    for directory, directory_names, file_names in os.walk(
        target,
        followlinks=False,
        onerror=_raise_walk_error,
    ):
        root = Path(directory)
        directory_names[:] = sorted(
            name for name in directory_names if not (root / name).is_symlink()
        )
        for name in sorted(file_names):
            candidate = root / name
            if not candidate.is_symlink():
                yield candidate


def _sha256(path: Path, max_bytes: int) -> str | None:
    digest = hashlib.sha256()
    total_bytes = 0
    with path.open("rb") as source:
        while chunk := source.read(_READ_CHUNK_BYTES):
            total_bytes += len(chunk)
            if total_bytes > max_bytes:
                return None
            digest.update(chunk)
    return digest.hexdigest()


class FileAuditCollector:
    name = "file_audit"

    def __init__(
        self,
        audit_paths: Sequence[Path],
        max_files: int,
        max_file_bytes: int,
    ) -> None:
        if max_files < 1:
            raise ValueError("max_files must be at least 1")
        if max_file_bytes < 1:
            raise ValueError("max_file_bytes must be at least 1")
        self._audit_paths = tuple(audit_paths)
        self._max_files = max_files
        self._max_file_bytes = max_file_bytes

    def collect(self) -> CollectorPayload:
        if not self._audit_paths:
            return CollectorPayload(
                data={"file_audit": []},
                status=CollectorStatus.DISABLED,
            )

        records: list[JSONValue] = []
        partial = False
        limit_reached = False
        for audit_path in self._audit_paths:
            try:
                if audit_path.is_symlink() or not audit_path.exists():
                    partial = True
                    continue
                candidates = _iter_regular_files(audit_path)
                for candidate in candidates:
                    if len(records) >= self._max_files:
                        limit_reached = True
                        break
                    try:
                        metadata = candidate.stat()
                        if metadata.st_size > self._max_file_bytes:
                            partial = True
                            continue
                        digest = _sha256(candidate, self._max_file_bytes)
                        if digest is None:
                            partial = True
                            continue
                        records.append(
                            {
                                "path": str(candidate),
                                "size_bytes": metadata.st_size,
                                "modified": datetime.fromtimestamp(
                                    metadata.st_mtime,
                                    tz=UTC,
                                ).isoformat(),
                                "sha256": digest,
                            }
                        )
                    except (OSError, OverflowError, ValueError):
                        partial = True
                if limit_reached:
                    break
            except OSError:
                partial = True

        payload_data: dict[str, JSONValue] = {"file_audit": records}
        if partial:
            return CollectorPayload(
                data=payload_data,
                status=CollectorStatus.PARTIAL,
                error_code=_PARTIAL_ERROR_CODE,
                error_message=_PARTIAL_ERROR_MESSAGE,
            )
        return CollectorPayload(data=payload_data)
