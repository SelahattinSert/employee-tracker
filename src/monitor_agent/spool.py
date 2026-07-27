"""Crash-consistent, bounded storage for telemetry awaiting delivery."""

from __future__ import annotations

import errno
import hashlib
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
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_CLOEXEC = getattr(os, "O_CLOEXEC", 0)


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
        """Add one complete record without ever exposing a partial ``.json`` file."""
        with self._lock:
            timestamp = self._timestamp(now)
            name = self._record_name(payload, timestamp)
            encoded = self._serialize(payload)
            self._ensure_directories()
            temporary = self._write_staged(encoded)
            try:
                destination = self._publish_staged(temporary, name)
            except BaseException:
                self._unlink_if_exists(temporary)
                raise
            self.enforce_retention(now=timestamp)
            return destination

    def pending(self) -> list[Path]:
        """Return independently owned pending records in deterministic oldest-first order."""
        with self._lock:
            self._ensure_directories()
            return self._pending_paths()

    def load(self, path: Path) -> dict[str, JSONValue] | None:
        """Read a valid pending record and quarantine invalid input."""
        with self._lock:
            candidate = self._pending_path(path)
            if not candidate.exists():
                return None
            try:
                decoded = self._read_record(candidate)
            except (OSError, json.JSONDecodeError, UnicodeDecodeError):
                self._move_to_dead_letter(candidate)
                return None
            if not self._valid_payload(decoded):
                self._move_to_dead_letter(candidate)
                return None
            return cast(dict[str, JSONValue], decoded)

    def ack(self, path: Path) -> None:
        """Remove a verified pending record after successful transmission."""
        with self._lock:
            candidate = self._pending_path(path)
            if not candidate.exists():
                return
            self._validated_record_stat(candidate)
            self._unlink_name(self.root, candidate.name)
            self._fsync_directory(self.root)

    def reject(self, path: Path) -> Path:
        """Move a record to an attributable, idempotent dead-letter destination."""
        with self._lock:
            candidate = self._pending_path(path)
            if not candidate.exists():
                return self._existing_rejection(candidate)
            return self._move_to_dead_letter(candidate, suffix=".rejected")

    def enforce_retention(self, *, now: datetime | None = None) -> RetentionResult:
        """Evict expired records, then oldest records until pending bytes fit the cap."""
        with self._lock:
            self._ensure_directories()
            cutoff = self._timestamp(now) - timedelta(seconds=self.max_age_sec)
            evicted_count = 0
            evicted_bytes = 0
            changed = False

            for record in self._pending_paths():
                if self._record_time(record) < cutoff:
                    size = self._validated_record_stat(record).st_size
                    self._unlink_name(self.root, record.name)
                    evicted_count += 1
                    evicted_bytes += size
                    changed = True

            records = self._pending_paths()
            pending_bytes = sum(self._validated_record_stat(record).st_size for record in records)
            for record in records:
                if pending_bytes <= self.max_bytes:
                    break
                size = self._validated_record_stat(record).st_size
                self._unlink_name(self.root, record.name)
                pending_bytes -= size
                evicted_count += 1
                evicted_bytes += size
                changed = True

            if changed:
                self._fsync_directory(self.root)
            return RetentionResult(evicted_count=evicted_count, evicted_bytes=evicted_bytes)

    def stats(self) -> SpoolStats:
        """Return counts and bytes for independently owned spool records."""
        with self._lock:
            self._ensure_directories()
            pending = self._pending_paths()
            dead_letter_count = sum(
                1 for path in self._dead_letter.glob("*.json") if self._is_regular_file(path)
            )
            return SpoolStats(
                pending_count=len(pending),
                pending_bytes=sum(self._validated_record_stat(path).st_size for path in pending),
                dead_letter_count=dead_letter_count,
            )

    def _ensure_directories(self) -> None:
        for directory in (self.root, self._dead_letter):
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            mode = os.lstat(directory).st_mode
            if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
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
        return f"{now.strftime('%Y%m%dT%H%M%S%fZ')}_{event_id}.json"

    @staticmethod
    def _serialize(payload: Mapping[str, JSONValue]) -> bytes:
        try:
            encoded = json.dumps(
                payload,
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
        except (TypeError, ValueError) as error:
            raise TypeError("payload is not JSON serializable") from error
        return encoded.encode("utf-8")

    def _write_staged(self, encoded: bytes) -> Path:
        descriptor, raw_path = tempfile.mkstemp(dir=self.root, prefix=".spool-", suffix=".tmp")
        temporary = Path(raw_path)
        try:
            with os.fdopen(descriptor, "wb", closefd=True) as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, 0o600)
        except BaseException:
            self._unlink_if_exists(temporary)
            raise
        return temporary

    def _publish_staged(self, temporary: Path, name: str) -> Path:
        for number in range(1_000_000):
            destination = self.root / self._numbered_name(name, number)
            try:
                self._link_name(self.root, temporary.name, self.root, destination.name)
            except FileExistsError:
                continue
            except OSError:
                if os.name != "nt":
                    raise
                self._rename_windows_no_overwrite(
                    temporary, destination
                )  # pragma: no cover - Windows
            self._unlink_name(self.root, temporary.name)
            self._fsync_directory(self.root)
            return destination
        raise OSError("could not publish a unique spool record")  # pragma: no cover - bounded space

    def _pending_paths(self) -> list[Path]:
        records = [
            path
            for path in self.root.glob("*.json")
            if path.parent == self.root and self._is_regular_file(path)
        ]
        return sorted(records, key=lambda path: (self._record_time(path), path.name))

    @staticmethod
    def _is_regular_file(path: Path) -> bool:
        try:
            record_stat = os.lstat(path)
        except OSError:
            return False
        return stat.S_ISREG(record_stat.st_mode) and record_stat.st_nlink == 1

    def _pending_path(self, path: Path) -> Path:
        candidate = Path(path).absolute()
        if (
            candidate.parent != self.root
            or candidate.name != Path(path).name
            or candidate.suffix != ".json"
        ):
            raise ValueError("not a pending spool record")
        if candidate.exists():
            self._validated_record_stat(candidate)
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

    def _read_record(self, record: Path) -> object:
        descriptor = self._open_record(self.root, record.name)
        with os.fdopen(descriptor, "r", encoding="utf-8", closefd=True) as handle:
            return json.load(handle)

    def _read_bytes(self, record: Path) -> bytes:
        descriptor = self._open_record(self.root, record.name)
        with os.fdopen(descriptor, "rb", closefd=True) as handle:
            return handle.read()

    def _validated_record_stat(self, path: Path) -> os.stat_result:
        try:
            record_stat = os.lstat(path)
        except FileNotFoundError:
            raise ValueError("not a pending spool record") from None
        if not stat.S_ISREG(record_stat.st_mode) or record_stat.st_nlink != 1:
            raise ValueError("not a pending spool record")
        return record_stat

    def _record_time(self, record: Path) -> datetime:
        timestamp = record.name.split("_", 1)[0]
        try:
            return datetime.strptime(timestamp, "%Y%m%dT%H%M%S%fZ").replace(tzinfo=UTC)
        except ValueError:
            return datetime.fromtimestamp(self._validated_record_stat(record).st_mtime, tz=UTC)

    def _move_to_dead_letter(self, source: Path, suffix: str = "") -> Path:
        source = self._pending_path(source)
        self._validated_record_stat(source)
        destination_name = self._unique_dead_letter_name(source, suffix)
        reservation_name = f".move-{destination_name}.tmp"
        final_destination = self._dead_letter / destination_name

        self._chmod_name(self.root, source.name, 0o600)
        self._reserve_name(self._dead_letter, reservation_name)
        moved = False
        try:
            self._replace_name(self.root, source.name, self._dead_letter, reservation_name)
            moved = True
            try:
                self._link_name(
                    self._dead_letter, reservation_name, self._dead_letter, destination_name
                )
            except FileExistsError:
                raise FileExistsError("dead-letter destination collided during publish") from None
            except OSError:
                if os.name != "nt":
                    raise
                self._rename_windows_no_overwrite(
                    self._dead_letter / reservation_name, final_destination
                )  # pragma: no cover - Windows
            self._unlink_name(self._dead_letter, reservation_name)
        except BaseException:
            if not moved:
                self._unlink_name(self._dead_letter, reservation_name)
            raise

        # The final destination is committed; durability errors must preserve it.
        self._fsync_directory(self.root)
        self._fsync_directory(self._dead_letter)
        return final_destination

    @staticmethod
    def _numbered_name(name: str, number: int) -> str:
        if number == 0:
            return name
        stem, extension = name.rsplit(".", 1)
        return f"{stem}.{number}.{extension}"

    @staticmethod
    def _attribution(source: Path) -> str:
        return hashlib.sha256(source.name.encode("utf-8")).hexdigest()[:16]

    def _dead_letter_name(self, source: Path, suffix: str) -> str:
        return f"{source.stem}.{self._attribution(source)}{suffix}.json"

    def _unique_dead_letter_name(self, source: Path, suffix: str) -> str:
        name = self._dead_letter_name(source, suffix)
        if not (self._dead_letter / name).exists():
            return name
        digest = hashlib.sha256(self._read_bytes(source)).hexdigest()[:16]
        stem, extension = name.rsplit(".", 1)
        return f"{stem}.{digest}.{extension}"

    def _existing_rejection(self, source: Path) -> Path:
        name = self._dead_letter_name(source, ".rejected")
        stem, extension = name.rsplit(".", 1)
        alternates = sorted(self._dead_letter.glob(f"{stem}.*.{extension}"))
        return alternates[0] if alternates else self._dead_letter / name

    def _reserve_name(self, directory: Path, name: str) -> None:
        descriptor = self._open_name(
            directory,
            name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW | _CLOEXEC,
            0o600,
        )
        os.close(descriptor)

    def _open_record(self, directory: Path, name: str) -> int:
        descriptor = self._open_name(directory, name, os.O_RDONLY | _NOFOLLOW | _CLOEXEC)
        try:
            record_stat = os.fstat(descriptor)
            if not stat.S_ISREG(record_stat.st_mode) or record_stat.st_nlink != 1:
                raise ValueError("not a pending spool record")
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise

    @staticmethod
    def _open_name(directory: Path, name: str, flags: int, mode: int = 0o600) -> int:
        if os.name != "posix":  # pragma: no cover - Windows fallback
            return os.open(directory / name, flags, mode)
        descriptor = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | _CLOEXEC)
        try:
            return os.open(name, flags, mode, dir_fd=descriptor)
        finally:
            os.close(descriptor)

    @staticmethod
    def _link_name(
        source_directory: Path, source_name: str, destination_directory: Path, destination_name: str
    ) -> None:
        if os.name != "posix":  # pragma: no cover - Windows fallback
            os.link(source_directory / source_name, destination_directory / destination_name)
            return
        source_fd = os.open(
            source_directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | _CLOEXEC
        )
        destination_fd = os.open(
            destination_directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | _CLOEXEC
        )
        try:
            os.link(
                source_name,
                destination_name,
                src_dir_fd=source_fd,
                dst_dir_fd=destination_fd,
                follow_symlinks=False,
            )
        finally:
            os.close(destination_fd)
            os.close(source_fd)

    @staticmethod
    def _replace_name(
        source_directory: Path, source_name: str, destination_directory: Path, destination_name: str
    ) -> None:
        if os.name != "posix":  # pragma: no cover - Windows fallback
            os.replace(source_directory / source_name, destination_directory / destination_name)
            return
        source_fd = os.open(
            source_directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | _CLOEXEC
        )
        destination_fd = os.open(
            destination_directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | _CLOEXEC
        )
        try:
            os.replace(
                source_name, destination_name, src_dir_fd=source_fd, dst_dir_fd=destination_fd
            )
        finally:
            os.close(destination_fd)
            os.close(source_fd)

    @staticmethod
    def _unlink_name(directory: Path, name: str) -> None:
        if os.name != "posix":  # pragma: no cover - Windows fallback
            with suppress(FileNotFoundError):
                (directory / name).unlink()
            return
        descriptor = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | _CLOEXEC)
        try:
            with suppress(FileNotFoundError):
                os.unlink(name, dir_fd=descriptor)
        finally:
            os.close(descriptor)

    @staticmethod
    def _chmod_name(directory: Path, name: str, mode: int) -> None:
        if os.name != "posix":  # pragma: no cover - Windows fallback
            os.chmod(directory / name, mode)
            return
        descriptor = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | _CLOEXEC)
        try:
            os.chmod(name, mode, dir_fd=descriptor, follow_symlinks=False)
        finally:
            os.close(descriptor)

    @staticmethod
    def _rename_windows_no_overwrite(source: Path, destination: Path) -> None:
        if destination.exists():
            raise FileExistsError(destination)
        os.rename(source, destination)

    @staticmethod
    def _unlink_if_exists(path: Path) -> None:
        with suppress(FileNotFoundError):
            path.unlink()

    @staticmethod
    def _fsync_directory(directory: Path) -> None:
        if not _supports_directory_fsync():
            return
        try:
            descriptor = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | _CLOEXEC)
        except OSError:
            return
        try:
            os.fsync(descriptor)
        except OSError as error:
            if error.errno not in _DIRECTORY_FSYNC_ERRORS:
                raise
        finally:
            os.close(descriptor)
