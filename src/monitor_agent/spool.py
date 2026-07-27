"""Atomic, bounded on-disk storage for telemetry awaiting delivery."""

from __future__ import annotations

import errno
import json
import os
import stat
import tempfile
from collections.abc import Mapping
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import RLock
from typing import cast
from uuid import UUID

from monitor_agent.models import JSONValue, RetentionResult, SpoolStats

_DEAD_LETTER = "dead-letter"
_DIRECTORY_FSYNC_ERRORS = {errno.EINVAL, errno.ENOTSUP, errno.EOPNOTSUPP}


def _supports_directory_fsync() -> bool:
    return os.name == "posix"


class Spool:
    """Persist JSON telemetry records safely until the transport acknowledges them."""

    def __init__(self, root: Path, max_bytes: int, max_age_sec: int) -> None:
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        if max_age_sec <= 0:
            raise ValueError("max_age_sec must be positive")

        self.root = Path(root).absolute()
        self.max_bytes = max_bytes
        self.max_age_sec = max_age_sec
        self._lock = RLock()
        self._dead_letter = self.root / _DEAD_LETTER
        with self._lock:
            self._ensure_directories()

    def enqueue(self, payload: Mapping[str, JSONValue], *, now: datetime | None = None) -> Path:
        """Atomically add a JSON payload and enforce the configured bounds."""
        with self._lock:
            timestamp = self._timestamp(now)
            name = self._record_name(payload, timestamp)
            encoded = self._serialize(payload)
            self._ensure_directories()

            temporary: Path | None = None
            reserved: Path | None = None
            destination: Path | None = None
            try:
                with tempfile.NamedTemporaryFile(
                    mode="wb", dir=self.root, prefix=".spool-", suffix=".tmp", delete=False
                ) as handle:
                    temporary = Path(handle.name)
                    handle.write(encoded)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.chmod(temporary, 0o600)
                destination = self._reserve_record_path(name)
                reserved = destination
                os.replace(temporary, destination)
                temporary = None
                reserved = None
                self._fsync_directory(self.root)
            finally:
                if temporary is not None:
                    self._unlink_if_exists(temporary)
                if reserved is not None:
                    self._unlink_if_exists(reserved)

            self.enforce_retention(now=timestamp)
            if destination is None:  # pragma: no cover - protects the return type after I/O failure
                raise RuntimeError("spool destination was not created")
            return destination

    def pending(self) -> list[Path]:
        """Return regular pending records in deterministic oldest-first order."""
        with self._lock:
            self._ensure_directories()
            return self._pending_paths()

    def load(self, path: Path) -> dict[str, JSONValue] | None:
        """Read a valid pending record, dead-lettering invalid JSON records."""
        with self._lock:
            candidate = self._pending_path(path)
            if not candidate.exists():
                return None
            try:
                with candidate.open("r", encoding="utf-8") as handle:
                    decoded = json.load(handle)
            except (OSError, json.JSONDecodeError, UnicodeDecodeError):
                self._move_to_dead_letter(candidate)
                return None

            if not self._valid_payload(decoded):
                self._move_to_dead_letter(candidate)
                return None
            return cast(dict[str, JSONValue], decoded)

    def ack(self, path: Path) -> None:
        """Delete a pending record after successful transmission."""
        with self._lock:
            candidate = self._pending_path(path)
            if not candidate.exists():
                return
            candidate.unlink()
            self._fsync_directory(self.root)

    def reject(self, path: Path) -> Path:
        """Move a pending record to dead-letter storage after permanent rejection."""
        with self._lock:
            candidate = self._pending_path(path)
            if not candidate.exists():
                previous = self._existing_dead_letter(candidate, suffix=".rejected")
                if previous is not None:
                    return previous
                return self._dead_letter_path(candidate, ".rejected", 0)
            return self._move_to_dead_letter(candidate, suffix=".rejected")

    def enforce_retention(self, *, now: datetime | None = None) -> RetentionResult:
        """Evict expired records, then oldest records until pending bytes fit the cap."""
        with self._lock:
            self._ensure_directories()
            timestamp = self._timestamp(now)
            cutoff = timestamp - timedelta(seconds=self.max_age_sec)
            evicted_count = 0
            evicted_bytes = 0
            directory_changed = False

            records = self._pending_paths()
            for record in records:
                if self._record_time(record) < cutoff:
                    size = record.stat().st_size
                    record.unlink()
                    evicted_count += 1
                    evicted_bytes += size
                    directory_changed = True

            records = self._pending_paths()
            pending_bytes = sum(record.stat().st_size for record in records)
            for record in records:
                if pending_bytes <= self.max_bytes:
                    break
                size = record.stat().st_size
                record.unlink()
                pending_bytes -= size
                evicted_count += 1
                evicted_bytes += size
                directory_changed = True

            if directory_changed:
                self._fsync_directory(self.root)
            return RetentionResult(evicted_count=evicted_count, evicted_bytes=evicted_bytes)

    def stats(self) -> SpoolStats:
        """Return pending-record bytes and dead-letter record count."""
        with self._lock:
            self._ensure_directories()
            pending = self._pending_paths()
            dead_letter_count = sum(
                1
                for path in self._dead_letter.glob("*.json")
                if self._is_regular_file(path)
            )
            return SpoolStats(
                pending_count=len(pending),
                pending_bytes=sum(path.stat().st_size for path in pending),
                dead_letter_count=dead_letter_count,
            )

    def _ensure_directories(self) -> None:
        for directory in (self.root, self._dead_letter):
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            if directory.is_symlink() or not directory.is_dir():
                raise ValueError("spool directory must be a real directory")
            os.chmod(directory, 0o700)

    @staticmethod
    def _timestamp(now: datetime | None) -> datetime:
        timestamp = datetime.now(UTC) if now is None else now
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        return timestamp.astimezone(UTC)

    @staticmethod
    def _record_name(payload: Mapping[str, JSONValue], now: datetime) -> str:
        if not isinstance(payload, Mapping):
            raise TypeError("payload must be a mapping")
        event_id = payload.get("event_id")
        if not isinstance(event_id, str):
            raise ValueError("event_id must be a UUID")
        try:
            UUID(event_id)
        except (AttributeError, TypeError, ValueError) as error:
            raise ValueError("event_id must be a UUID") from error
        timestamp = now.strftime("%Y%m%dT%H%M%S%fZ")
        return f"{timestamp}_{event_id}.json"

    @staticmethod
    def _serialize(payload: Mapping[str, JSONValue]) -> bytes:
        try:
            serialized = json.dumps(
                payload,
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
        except (TypeError, ValueError) as error:
            raise TypeError("payload is not JSON serializable") from error
        return serialized.encode("utf-8")

    def _pending_paths(self) -> list[Path]:
        records = [
            path
            for path in self.root.glob("*.json")
            if self._is_regular_file(path) and path.parent == self.root
        ]
        return sorted(records, key=lambda path: (self._record_time(path), path.name))

    @staticmethod
    def _is_regular_file(path: Path) -> bool:
        try:
            return not path.is_symlink() and stat.S_ISREG(path.stat().st_mode)
        except OSError:
            return False

    def _pending_path(self, path: Path) -> Path:
        candidate = Path(path).absolute()
        if candidate.parent != self.root or candidate.suffix != ".json":
            raise ValueError("not a pending spool record")
        if candidate.is_symlink():
            raise ValueError("not a pending spool record")
        if candidate.exists() and not self._is_regular_file(candidate):
            raise ValueError("not a pending spool record")
        return candidate

    @staticmethod
    def _valid_payload(decoded: object) -> bool:
        if not isinstance(decoded, dict):
            return False
        event_id = decoded.get("event_id")
        if not isinstance(event_id, str):
            return False
        try:
            UUID(event_id)
        except (AttributeError, TypeError, ValueError):
            return False
        return True

    def _record_time(self, record: Path) -> datetime:
        timestamp = record.name.split("_", 1)[0]
        try:
            return datetime.strptime(timestamp, "%Y%m%dT%H%M%S%fZ").replace(tzinfo=UTC)
        except ValueError:
            return datetime.fromtimestamp(record.stat().st_mtime, tz=UTC)

    def _reserve_record_path(self, name: str) -> Path:
        stem = Path(name).stem
        for number in range(1_000_000):
            candidate = self.root / (name if number == 0 else f"{stem}.{number}.json")
            try:
                descriptor = os.open(candidate, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                continue
            os.close(descriptor)
            return candidate
        raise OSError("could not reserve a unique spool record name")

    def _move_to_dead_letter(self, source: Path, suffix: str = "") -> Path:
        source = self._pending_path(source)
        if not source.exists() or not self._is_regular_file(source):
            raise ValueError("not a pending spool record")
        for number in range(1_000_000):
            destination = self._dead_letter_path(source, suffix, number)
            try:
                os.link(source, destination, follow_symlinks=False)
            except FileExistsError:
                continue
            except (NotImplementedError, OSError) as error:
                if isinstance(error, OSError) and error.errno not in {errno.EPERM, errno.EXDEV}:
                    raise
                return self._replace_into_unique_destination(source, suffix)
            try:
                os.chmod(destination, 0o600)
                source.unlink()
            except BaseException:
                self._unlink_if_exists(destination)
                raise
            self._fsync_directory(self.root)
            self._fsync_directory(self._dead_letter)
            return destination
        raise OSError("could not reserve a unique dead-letter record name")

    def _replace_into_unique_destination(self, source: Path, suffix: str) -> Path:
        for number in range(1_000_000):
            destination = self._dead_letter_path(source, suffix, number)
            try:
                descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                continue
            os.close(descriptor)
            try:
                os.replace(source, destination)
                os.chmod(destination, 0o600)
            except BaseException:
                self._unlink_if_exists(destination)
                raise
            self._fsync_directory(self.root)
            self._fsync_directory(self._dead_letter)
            return destination
        raise OSError("could not reserve a unique dead-letter record name")

    def _dead_letter_path(self, source: Path, suffix: str, number: int) -> Path:
        index = "" if number == 0 else f".{number}"
        return self._dead_letter / f"{source.stem}{index}{suffix}.json"

    def _existing_dead_letter(self, source: Path, suffix: str) -> Path | None:
        prefix = f"{source.stem}."
        expected = self._dead_letter_path(source, suffix, 0)
        candidates = [expected, *sorted(self._dead_letter.glob(f"{source.stem}.*{suffix}.json"))]
        for candidate in candidates:
            if self._is_regular_file(candidate) and (
                candidate == expected or candidate.name.startswith(prefix)
            ):
                return candidate
        return None

    @staticmethod
    def _unlink_if_exists(path: Path) -> None:
        with suppress(FileNotFoundError):
            path.unlink()

    @staticmethod
    def _fsync_directory(directory: Path) -> None:
        if not _supports_directory_fsync():
            return
        try:
            descriptor = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        except OSError:
            return
        try:
            os.fsync(descriptor)
        except OSError as error:
            if error.errno not in _DIRECTORY_FSYNC_ERRORS:
                raise
        finally:
            os.close(descriptor)
