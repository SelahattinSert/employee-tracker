"""Crash-consistent, bounded storage for telemetry awaiting delivery."""

from __future__ import annotations

import errno
import hashlib
import importlib
import json
import os
import stat
import sys
import tempfile
from collections.abc import Mapping
from contextlib import suppress
from ctypes import CDLL, c_char_p, c_int, get_errno
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import RLock
from typing import Protocol, cast
from uuid import UUID

from monitor_agent.models import JSONValue, RetentionResult, SpoolStats

_DEAD_LETTER = "dead-letter"
_DIRECTORY_FSYNC_ERRORS = {errno.EINVAL, errno.ENOTSUP, errno.EOPNOTSUPP}
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_RENAME_NOREPLACE = 1
_LOCK_BUSY_ERRORS = {errno.EACCES, errno.EAGAIN, errno.EDEADLK}
_ARTIFACT_GUARD_PREFIX = ".artifact-guard-"


class _WindowsLockingModule(Protocol):
    LK_LOCK: int
    LK_NBLCK: int
    LK_UNLCK: int

    def locking(self, descriptor: int, mode: int, length: int) -> None: ...


def _platform_name() -> str:
    return os.name


def _windows_locking() -> _WindowsLockingModule:
    return cast(_WindowsLockingModule, importlib.import_module("msvcrt"))


def _supports_directory_fsync() -> bool:
    return _platform_name() == "posix"


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
        self._artifact_guards: dict[tuple[Path, str], tuple[int, int]] = {}
        with self._lock:
            self._prepare()

    def enqueue(self, payload: Mapping[str, JSONValue], *, now: datetime | None = None) -> Path:
        """Add one complete record without ever exposing a partial ``.json`` file."""
        with self._lock:
            timestamp = self._timestamp(now)
            name = self._record_name(payload, timestamp)
            encoded = self._serialize(payload)
            self._prepare()
            temporary: Path | None = None
            descriptor = -1
            guard_held = False
            try:
                temporary, descriptor = self._write_staged(encoded)
                guard_held = True
                if _platform_name() != "posix":  # pragma: no cover - Windows fallback
                    self._close_locked_descriptor(descriptor)
                    descriptor = -1
                destination = self._publish_staged(temporary, name)
            except BaseException:
                try:
                    if descriptor >= 0:
                        self._close_locked_descriptor_with_retry(descriptor)
                    if temporary is not None:
                        self._unlink_name(self.root, temporary.name)
                except BaseException:
                    pass
                finally:
                    if guard_held:
                        with suppress(BaseException):
                            self._release_artifact_guard(self.root, ".spool-")
                raise
            try:
                if descriptor >= 0:
                    self._close_locked_descriptor_with_retry(descriptor)
            finally:
                self._release_artifact_guard(self.root, ".spool-")
            self.enforce_retention(now=timestamp)
            return destination

    def pending(self) -> list[Path]:
        """Return independently owned pending records in deterministic oldest-first order."""
        with self._lock:
            self._prepare()
            return self._pending_paths()

    def load(self, path: Path) -> dict[str, JSONValue] | None:
        """Read a valid pending record and quarantine invalid input."""
        with self._lock:
            self._prepare()
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
            self._prepare()
            candidate = self._pending_path(path)
            if not candidate.exists():
                return
            self._validated_record_stat(candidate)
            self._unlink_name(self.root, candidate.name)
            self._fsync_directory(self.root)

    def reject(self, path: Path) -> Path:
        """Move a record to an attributable, idempotent dead-letter destination."""
        with self._lock:
            self._prepare()
            candidate = self._pending_path(path)
            if not candidate.exists():
                return self._existing_rejection(candidate)
            return self._move_to_dead_letter(candidate, suffix=".rejected")

    def enforce_retention(self, *, now: datetime | None = None) -> RetentionResult:
        """Evict expired records, then oldest records until pending bytes fit the cap."""
        with self._lock:
            self._prepare()
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
            self._prepare()
            pending = self._pending_paths()
            dead_letter_count = sum(
                1
                for path in self._dead_letter.glob("*.json")
                if not path.name.startswith(".") and self._is_regular_file(path)
            )
            return SpoolStats(
                pending_count=len(pending),
                pending_bytes=sum(self._validated_record_stat(path).st_size for path in pending),
                dead_letter_count=dead_letter_count,
            )

    def _prepare(self) -> None:
        self._ensure_directories()
        self._recover_dead_letter_links()
        self._recover_pending_links()
        self._cleanup_hidden_artifacts()

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

    def _write_staged(self, encoded: bytes) -> tuple[Path, int]:
        if not self._acquire_artifact_guard(
            self.root, ".spool-", blocking=True
        ):
            raise OSError(errno.EAGAIN, "spool artifact guard is busy")
        try:
            descriptor, raw_path = tempfile.mkstemp(
                dir=self.root, prefix=".spool-", suffix=".tmp"
            )
        except BaseException:
            self._release_artifact_guard(self.root, ".spool-")
            raise
        temporary = Path(raw_path)
        try:
            self._lock_descriptor(descriptor)
            self._fchmod_descriptor(descriptor, temporary, 0o600)
            with os.fdopen(os.dup(descriptor), "wb", closefd=True) as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
        except BaseException:
            try:
                try:
                    self._close_locked_descriptor_with_retry(descriptor)
                finally:
                    try:
                        self._unlink_if_exists(temporary)
                    finally:
                        self._release_artifact_guard(self.root, ".spool-")
            except BaseException:
                pass
            raise
        return temporary, descriptor

    def _publish_staged(self, temporary: Path, name: str) -> Path:
        for number in range(1_000_000):
            destination = self.root / self._numbered_name(name, number)
            reservation_name = self._publish_reservation_name(destination.name)
            reservation = self._acquire_reservation(self.root, reservation_name)
            if reservation is None:
                continue
            try:
                self._publish_noreplace(
                    self.root, temporary.name, self.root, destination.name
                )
                self._fsync_directory(self.root)
            except FileExistsError:
                self._release_reservation(
                    self.root, reservation_name, reservation
                )
                continue
            except BaseException:
                try:
                    self._close_locked_descriptor_with_retry(reservation)
                except BaseException:
                    pass
                finally:
                    with suppress(BaseException):
                        self._release_artifact_guard(self.root, reservation_name)
                raise
            self._release_reservation(self.root, reservation_name, reservation)
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
        source_descriptor = self._open_record(self.root, source.name)
        source_locked = False
        try:
            if _platform_name() == "posix":
                self._lock_descriptor(source_descriptor)
                source_locked = True
            self._fchmod_descriptor(source_descriptor, source, 0o600)
            source_digest = self._descriptor_digest(source_descriptor)
            operation_id = self._operation_id(source.name, suffix)
            operation_lock_name = f".deadop-{operation_id}.lock"
            operation_lock = self._acquire_reservation(
                self._dead_letter, operation_lock_name
            )
            if operation_lock is None:
                raise OSError(errno.EAGAIN, "dead-letter operation is already in progress")

            try:
                marker_name = f".deadop-{operation_id}.json"
                marker = self._read_operation_marker(
                    marker_name, source.name, suffix
                )
                destination_lock: int | None = None
                destination_lock_name = ""
                if marker is None:
                    marker_committed = False
                    destination_name, destination_lock_name, destination_lock = (
                        self._reserve_dead_letter_destination(source, suffix)
                    )
                    try:
                        self._persist_operation_marker(
                            marker_name,
                            source.name,
                            suffix,
                            destination_name,
                            source_digest,
                        )
                    except BaseException:
                        self._release_reservation(
                            self._dead_letter, destination_lock_name, destination_lock
                        )
                        destination_lock = None
                        raise
                else:
                    destination_name, marker_digest, marker_committed = marker
                    if marker_digest != source_digest:
                        raise ValueError(
                            "dead-letter operation marker does not match source content"
                        )

                while True:
                    final_destination = self._dead_letter / destination_name
                    if self._name_exists(self._dead_letter, destination_name):
                        if marker_committed and self._destination_matches_digest(
                            final_destination, source_digest
                        ):
                            return final_destination
                        self._release_reservation(
                            self._dead_letter,
                            destination_lock_name,
                            destination_lock,
                        )
                        destination_lock = None
                        destination_lock_name = ""
                        (
                            destination_name,
                            destination_lock_name,
                            destination_lock,
                        ) = self._reserve_dead_letter_destination(source, suffix)
                        try:
                            self._persist_operation_marker(
                                marker_name,
                                source.name,
                                suffix,
                                destination_name,
                                source_digest,
                                replace_existing=True,
                            )
                        except BaseException:
                            self._release_reservation(
                                self._dead_letter,
                                destination_lock_name,
                                destination_lock,
                            )
                            destination_lock = None
                            raise
                        marker_committed = False
                        continue

                    if destination_lock is None:
                        destination_lock_name = self._dead_letter_reservation_name(
                            destination_name
                        )
                        destination_lock = self._acquire_reservation(
                            self._dead_letter, destination_lock_name
                        )
                    if destination_lock is None:
                        if self._name_exists(self._dead_letter, destination_name):
                            continue
                        raise OSError(
                            errno.EAGAIN, "dead-letter destination is reserved"
                        )

                    if not self._open_name_matches(
                        self.root, source.name, source_descriptor
                    ):
                        raise ValueError("dead-letter source changed before commit")

                    if _platform_name() != "posix":  # pragma: no cover - Windows fallback
                        os.close(source_descriptor)
                        source_descriptor = -1
                    try:
                        self._publish_noreplace(
                            self.root,
                            source.name,
                            self._dead_letter,
                            destination_name,
                        )
                    except FileExistsError:
                        self._release_reservation(
                            self._dead_letter,
                            destination_lock_name,
                            destination_lock,
                        )
                        destination_lock = None
                        destination_lock_name = ""
                        if source_descriptor < 0:
                            source_descriptor = self._open_record(
                                self.root, source.name
                            )
                        continue
                    self._fsync_directory(self.root)
                    self._fsync_directory(self._dead_letter)
                    self._validate_dead_letter_destination(
                        final_destination, source_digest
                    )
                    self._persist_operation_marker(
                        marker_name,
                        source.name,
                        suffix,
                        destination_name,
                        source_digest,
                        committed=True,
                        replace_existing=True,
                    )
                    return final_destination
            finally:
                self._release_reservation(
                    self._dead_letter,
                    destination_lock_name,
                    destination_lock,
                )
                self._release_reservation(
                    self._dead_letter, operation_lock_name, operation_lock
                )
        finally:
            if source_descriptor >= 0:
                if source_locked:
                    self._close_locked_descriptor_with_retry(source_descriptor)
                else:
                    os.close(source_descriptor)

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

    def _existing_rejection(self, source: Path) -> Path:
        suffix = ".rejected"
        operation_id = self._operation_id(source.name, suffix)
        marker_name = f".deadop-{operation_id}.json"
        marker = self._read_operation_marker(marker_name, source.name, suffix)
        if marker is not None:
            destination_name, expected_digest, _ = marker
            destination = self._dead_letter / destination_name
            if self._name_exists(self._dead_letter, destination_name):
                self._validate_dead_letter_destination(destination, expected_digest)
            return destination
        return self._dead_letter / self._dead_letter_name(source, suffix)

    @staticmethod
    def _operation_id(source_name: str, suffix: str) -> str:
        material = f"{source_name}\0{suffix}".encode()
        return hashlib.sha256(material).hexdigest()[:24]

    @staticmethod
    def _publish_reservation_name(destination_name: str) -> str:
        digest = hashlib.sha256(destination_name.encode("utf-8")).hexdigest()[:24]
        return f".publish-{digest}.lock"

    @staticmethod
    def _dead_letter_reservation_name(destination_name: str) -> str:
        digest = hashlib.sha256(destination_name.encode("utf-8")).hexdigest()[:24]
        return f".deadres-{digest}.lock"

    def _reserve_dead_letter_destination(
        self, source: Path, suffix: str
    ) -> tuple[str, str, int]:
        canonical = self._dead_letter_name(source, suffix)
        for number in range(1_000_000):
            destination_name = self._numbered_name(canonical, number)
            reservation_name = self._dead_letter_reservation_name(destination_name)
            reservation = self._acquire_reservation(self._dead_letter, reservation_name)
            if reservation is None:
                continue
            if self._name_exists(self._dead_letter, destination_name):
                self._release_reservation(
                    self._dead_letter, reservation_name, reservation
                )
                continue
            return destination_name, reservation_name, reservation
        raise OSError("could not reserve a unique dead-letter destination")

    def _persist_operation_marker(
        self,
        marker_name: str,
        source_name: str,
        suffix: str,
        destination_name: str,
        source_digest: str,
        *,
        committed: bool = False,
        replace_existing: bool = False,
    ) -> None:
        if not self._acquire_artifact_guard(
            self._dead_letter, ".deadmark-", blocking=True
        ):
            raise OSError(errno.EAGAIN, "dead-letter marker guard is busy")
        try:
            encoded = json.dumps(
                {
                    "committed": committed,
                    "destination": destination_name,
                    "sha256": source_digest,
                    "source": source_name,
                    "suffix": suffix,
                },
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
            descriptor, raw_path = tempfile.mkstemp(
                dir=self._dead_letter, prefix=".deadmark-", suffix=".tmp"
            )
            temporary = Path(raw_path)
            try:
                self._fchmod_descriptor(descriptor, temporary, 0o600)
                handle = os.fdopen(descriptor, "wb", closefd=True)
                descriptor = -1
                with handle:
                    handle.write(encoded)
                    handle.flush()
                    os.fsync(handle.fileno())
                if not replace_existing and self._name_exists(
                    self._dead_letter, marker_name
                ):
                    raise FileExistsError(
                        "dead-letter operation marker already exists"
                    )
                self._replace_name(
                    self._dead_letter,
                    temporary.name,
                    self._dead_letter,
                    marker_name,
                )
                self._fsync_directory(self._dead_letter)
            except BaseException:
                if descriptor >= 0:
                    os.close(descriptor)
                self._unlink_name(self._dead_letter, temporary.name)
                raise
        finally:
            self._release_artifact_guard(self._dead_letter, ".deadmark-")

    def _read_operation_marker(
        self, marker_name: str, source_name: str, suffix: str
    ) -> tuple[str, str, bool] | None:
        if not self._name_exists(self._dead_letter, marker_name):
            return None
        decoded = self._decode_operation_marker(marker_name)
        if decoded.get("source") != source_name or decoded.get("suffix") != suffix:
            raise ValueError("dead-letter operation marker does not match source")
        destination = decoded.get("destination")
        source_digest = decoded.get("sha256")
        committed = decoded.get("committed")
        if (
            not isinstance(destination, str)
            or Path(destination).name != destination
            or destination.startswith(".")
            or not destination.endswith(".json")
            or not isinstance(source_digest, str)
            or len(source_digest) != 64
            or any(character not in "0123456789abcdef" for character in source_digest)
            or not isinstance(committed, bool)
        ):
            raise ValueError("invalid dead-letter operation destination")
        return destination, source_digest, committed

    def _decode_operation_marker(self, marker_name: str) -> dict[str, object]:
        descriptor = self._open_record(self._dead_letter, marker_name)
        with os.fdopen(descriptor, "r", encoding="utf-8", closefd=True) as handle:
            try:
                decoded = json.load(handle)
            except (json.JSONDecodeError, UnicodeDecodeError) as error:
                raise ValueError("invalid dead-letter operation marker") from error
        if not isinstance(decoded, dict):
            raise ValueError("invalid dead-letter operation marker")
        return cast(dict[str, object], decoded)

    def _validate_dead_letter_destination(
        self, destination: Path, expected_digest: str
    ) -> None:
        descriptor = self._open_record(self._dead_letter, destination.name)
        try:
            if self._descriptor_digest(descriptor) != expected_digest:
                raise ValueError("dead-letter destination does not match operation")
        finally:
            os.close(descriptor)

    def _destination_matches_digest(
        self, destination: Path, expected_digest: str
    ) -> bool:
        try:
            self._validate_dead_letter_destination(destination, expected_digest)
        except (OSError, ValueError):
            return False
        return True

    @staticmethod
    def _descriptor_digest(descriptor: int) -> str:
        original_offset = os.lseek(descriptor, 0, os.SEEK_CUR)
        digest = hashlib.sha256()
        try:
            os.lseek(descriptor, 0, os.SEEK_SET)
            while chunk := os.read(descriptor, 65_536):
                digest.update(chunk)
        finally:
            os.lseek(descriptor, original_offset, os.SEEK_SET)
        return digest.hexdigest()

    def _recover_pending_links(self) -> None:
        for hidden in self.root.glob(".spool-*"):
            if not self._acquire_artifact_guard(
                self.root, hidden.name, blocking=False
            ):
                continue
            try:
                try:
                    descriptor = self._open_name(
                        self.root,
                        hidden.name,
                        os.O_RDONLY | _NOFOLLOW | _CLOEXEC,
                    )
                except OSError:
                    continue
                locked = False
                try:
                    if not self._try_lock_descriptor(descriptor):
                        continue
                    locked = True
                    hidden_stat = os.fstat(descriptor)
                    if (
                        not stat.S_ISREG(hidden_stat.st_mode)
                        or hidden_stat.st_nlink != 2
                    ):
                        continue
                    matches = []
                    for candidate in self.root.glob("*.json"):
                        try:
                            candidate_stat = os.lstat(candidate)
                        except FileNotFoundError:
                            continue
                        if (
                            stat.S_ISREG(candidate_stat.st_mode)
                            and candidate_stat.st_nlink == 2
                            and (candidate_stat.st_dev, candidate_stat.st_ino)
                            == (hidden_stat.st_dev, hidden_stat.st_ino)
                        ):
                            matches.append(candidate)
                    if len(matches) != 1:
                        continue
                    if self._unlink_open_name(self.root, hidden.name, descriptor):
                        self._fsync_directory(self.root)
                finally:
                    if locked:
                        self._close_locked_descriptor_with_retry(descriptor)
                    else:
                        os.close(descriptor)
            finally:
                self._release_artifact_guard(self.root, hidden.name)

    def _recover_dead_letter_links(self) -> None:
        for marker_path in self._dead_letter.glob(".deadop-*.json"):
            operation_lock_name = f"{marker_path.stem}.lock"
            if not self._acquire_artifact_guard(
                self._dead_letter, operation_lock_name, blocking=False
            ):
                continue
            try:
                decoded = self._decode_operation_marker(marker_path.name)
                source_name = decoded.get("source")
                suffix = decoded.get("suffix")
                destination_name = decoded.get("destination")
                expected_digest = decoded.get("sha256")
                committed = decoded.get("committed")
                if (
                    not isinstance(source_name, str)
                    or Path(source_name).name != source_name
                    or source_name.startswith(".")
                    or not source_name.endswith(".json")
                    or not isinstance(suffix, str)
                    or marker_path.name
                    != f".deadop-{self._operation_id(source_name, suffix)}.json"
                    or not isinstance(destination_name, str)
                    or Path(destination_name).name != destination_name
                    or destination_name.startswith(".")
                    or not destination_name.endswith(".json")
                    or not isinstance(expected_digest, str)
                    or len(expected_digest) != 64
                    or not isinstance(committed, bool)
                ):
                    raise ValueError("invalid dead-letter operation marker")
                source = self.root / source_name
                destination = self._dead_letter / destination_name
                try:
                    descriptor = self._open_name(
                        self.root,
                        source.name,
                        os.O_RDONLY | _NOFOLLOW | _CLOEXEC,
                    )
                except FileNotFoundError:
                    continue
                locked = False
                try:
                    if not self._try_lock_descriptor(descriptor):
                        continue
                    locked = True
                    source_stat = os.fstat(descriptor)
                    try:
                        destination_stat = os.lstat(destination)
                    except FileNotFoundError:
                        continue
                    if (
                        not stat.S_ISREG(source_stat.st_mode)
                        or not stat.S_ISREG(destination_stat.st_mode)
                        or source_stat.st_nlink != 2
                        or destination_stat.st_nlink != 2
                        or (source_stat.st_dev, source_stat.st_ino)
                        != (destination_stat.st_dev, destination_stat.st_ino)
                    ):
                        continue
                    if self._descriptor_digest(descriptor) != expected_digest:
                        raise ValueError(
                            "dead-letter recovery source does not match marker"
                        )
                    if self._unlink_open_name(self.root, source.name, descriptor):
                        self._fsync_directory(self.root)
                        self._fsync_directory(self._dead_letter)
                finally:
                    if locked:
                        self._close_locked_descriptor_with_retry(descriptor)
                    else:
                        os.close(descriptor)
            finally:
                self._release_artifact_guard(
                    self._dead_letter, operation_lock_name
                )

    def _cleanup_hidden_artifacts(self) -> None:
        root_prefixes = (".spool-", ".publish-")
        dead_letter_prefixes = (".deadmark-", ".deadop-", ".deadres-")
        for directory, prefixes in (
            (self.root, root_prefixes),
            (self._dead_letter, dead_letter_prefixes),
        ):
            for path in directory.iterdir():
                if not path.name.startswith(prefixes):
                    continue
                if path.name.startswith(".deadop-") and path.name.endswith(
                    ".json"
                ):
                    continue
                self._cleanup_stale_hidden_file(directory, path.name)

    def _cleanup_stale_hidden_file(self, directory: Path, name: str) -> bool:
        if not self._acquire_artifact_guard(directory, name, blocking=False):
            return False
        try:
            try:
                descriptor = self._open_name(
                    directory, name, os.O_RDWR | _NOFOLLOW | _CLOEXEC
                )
            except (FileNotFoundError, OSError):
                return False
            locked = False
            try:
                record_stat = os.fstat(descriptor)
                if (
                    not stat.S_ISREG(record_stat.st_mode)
                    or record_stat.st_nlink != 1
                ):
                    return False
                if not self._try_lock_descriptor(descriptor):
                    return False
                locked = True
                if _platform_name() != "posix":  # pragma: no cover - Windows fallback
                    self._close_locked_descriptor_with_retry(descriptor)
                    descriptor = -1
                    locked = False
                    try:
                        current_stat = os.lstat(directory / name)
                    except FileNotFoundError:
                        return False
                    if (
                        not stat.S_ISREG(current_stat.st_mode)
                        or (current_stat.st_dev, current_stat.st_ino)
                        != (record_stat.st_dev, record_stat.st_ino)
                    ):
                        return False
                    (directory / name).unlink()
                    self._fsync_directory(directory)
                    return True
                removed = self._unlink_open_name(directory, name, descriptor)
                if removed:
                    self._fsync_directory(directory)
                return removed
            finally:
                if descriptor >= 0:
                    if locked:
                        self._close_locked_descriptor_with_retry(descriptor)
                    else:
                        os.close(descriptor)
        finally:
            self._release_artifact_guard(directory, name)

    def _acquire_reservation(self, directory: Path, name: str) -> int | None:
        if not self._acquire_artifact_guard(directory, name, blocking=False):
            return None
        acquired = False
        try:
            for _ in range(2):
                try:
                    descriptor = self._open_name(
                        directory,
                        name,
                        os.O_RDWR
                        | os.O_CREAT
                        | os.O_EXCL
                        | _NOFOLLOW
                        | _CLOEXEC,
                        0o600,
                    )
                except FileExistsError:
                    if not self._cleanup_stale_hidden_file(directory, name):
                        return None
                    continue
                try:
                    self._lock_descriptor(descriptor)
                    self._fchmod_descriptor(descriptor, directory / name, 0o600)
                    acquired = True
                    return descriptor
                except BaseException:
                    with suppress(OSError):
                        self._unlock_descriptor(descriptor)
                    os.close(descriptor)
                    self._unlink_name(directory, name)
                    raise
            return None
        finally:
            if not acquired:
                self._release_artifact_guard(directory, name)

    def _release_reservation(
        self, directory: Path, name: str, descriptor: int | None
    ) -> None:
        if descriptor is None:
            return
        try:
            if _platform_name() != "posix":  # pragma: no cover - Windows fallback
                try:
                    self._close_locked_descriptor_with_retry(descriptor)
                finally:
                    self._unlink_name(directory, name)
            else:
                try:
                    self._unlink_name(directory, name)
                finally:
                    self._close_locked_descriptor_with_retry(descriptor)
        finally:
            self._release_artifact_guard(directory, name)

    @staticmethod
    def _artifact_guard_name(artifact_name: str) -> str:
        if artifact_name.startswith(".spool-"):
            return f"{_ARTIFACT_GUARD_PREFIX}spool"
        if artifact_name.startswith(".deadmark-"):
            return f"{_ARTIFACT_GUARD_PREFIX}deadmark"
        for prefix, kind in (
            (".publish-", "publish"),
            (".deadop-", "deadop"),
            (".deadres-", "deadres"),
        ):
            if artifact_name.startswith(prefix):
                slot = hashlib.sha256(artifact_name.encode("utf-8")).hexdigest()[:2]
                return f"{_ARTIFACT_GUARD_PREFIX}{kind}-{slot}"
        raise ValueError("unsupported hidden spool artifact")

    def _acquire_artifact_guard(
        self, directory: Path, artifact_name: str, *, blocking: bool
    ) -> bool:
        guard_name = self._artifact_guard_name(artifact_name)
        key = (directory.absolute(), guard_name)
        held = self._artifact_guards.get(key)
        if held is not None:
            descriptor, depth = held
            self._artifact_guards[key] = (descriptor, depth + 1)
            return True

        descriptor = self._open_name(
            directory,
            guard_name,
            os.O_RDWR | os.O_CREAT | _NOFOLLOW | _CLOEXEC,
            0o600,
        )
        locked = False
        try:
            record_stat = os.fstat(descriptor)
            if (
                not stat.S_ISREG(record_stat.st_mode)
                or record_stat.st_nlink != 1
                or not Spool._open_name_matches(
                    directory, guard_name, descriptor
                )
            ):
                raise ValueError("spool artifact guard must be a regular file")
            if blocking:
                self._lock_descriptor(descriptor)
                locked = True
            elif self._try_lock_descriptor(descriptor):
                locked = True
            else:
                return False
            if not Spool._open_name_matches(
                directory, guard_name, descriptor
            ):
                raise ValueError("spool artifact guard changed while locking")
            if stat.S_IMODE(record_stat.st_mode) != 0o600:
                self._fchmod_descriptor(
                    descriptor, directory / guard_name, 0o600
                )
            self._artifact_guards[key] = (descriptor, 1)
            return True
        finally:
            if key not in self._artifact_guards:
                if locked:
                    self._close_locked_descriptor_with_retry(descriptor)
                else:
                    os.close(descriptor)

    def _release_artifact_guard(
        self, directory: Path, artifact_name: str
    ) -> None:
        guard_name = self._artifact_guard_name(artifact_name)
        key = (directory.absolute(), guard_name)
        descriptor, depth = self._artifact_guards[key]
        if depth > 1:
            self._artifact_guards[key] = (descriptor, depth - 1)
            return
        del self._artifact_guards[key]
        self._close_locked_descriptor_with_retry(descriptor)

    def _close_locked_descriptor_with_retry(self, descriptor: int) -> None:
        try:
            self._close_locked_descriptor(descriptor)
        except BaseException:
            with suppress(BaseException):
                self._close_locked_descriptor(descriptor)
            raise

    @staticmethod
    def _lock_descriptor(descriptor: int) -> None:
        if _platform_name() == "nt":  # pragma: no cover - exercised through seam
            Spool._ensure_lock_byte(descriptor)
            locking = _windows_locking()
            locking.locking(descriptor, locking.LK_LOCK, 1)
            return
        if _platform_name() != "posix":  # pragma: no cover - conservative fallback
            return
        import fcntl

        fcntl.flock(descriptor, fcntl.LOCK_EX)

    @staticmethod
    def _try_lock_descriptor(descriptor: int) -> bool:
        if _platform_name() == "nt":  # pragma: no cover - exercised through seam
            Spool._ensure_lock_byte(descriptor)
            locking = _windows_locking()
            try:
                locking.locking(descriptor, locking.LK_NBLCK, 1)
            except OSError as error:
                if error.errno in _LOCK_BUSY_ERRORS:
                    return False
                raise
            return True
        if _platform_name() != "posix":  # pragma: no cover - conservative fallback
            return False
        import fcntl

        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        return True

    @staticmethod
    def _unlock_descriptor(descriptor: int) -> None:
        if _platform_name() == "nt":  # pragma: no cover - exercised through seam
            os.lseek(descriptor, 0, os.SEEK_SET)
            locking = _windows_locking()
            locking.locking(descriptor, locking.LK_UNLCK, 1)
            return
        if _platform_name() != "posix":  # pragma: no cover - conservative fallback
            return
        import fcntl

        fcntl.flock(descriptor, fcntl.LOCK_UN)

    @staticmethod
    def _ensure_lock_byte(descriptor: int) -> None:
        if os.fstat(descriptor).st_size == 0:
            os.lseek(descriptor, 0, os.SEEK_SET)
            os.write(descriptor, b"\0")
        os.lseek(descriptor, 0, os.SEEK_SET)

    @staticmethod
    def _close_locked_descriptor(descriptor: int) -> None:
        try:
            Spool._unlock_descriptor(descriptor)
        finally:
            os.close(descriptor)

    @staticmethod
    def _name_exists(directory: Path, name: str) -> bool:
        if _platform_name() != "posix":  # pragma: no cover - Windows fallback
            return (directory / name).exists()
        descriptor = os.open(directory, os.O_RDONLY | _DIRECTORY | _CLOEXEC)
        try:
            try:
                os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            except FileNotFoundError:
                return False
            return True
        finally:
            os.close(descriptor)

    @staticmethod
    def _open_name_matches(directory: Path, name: str, opened_descriptor: int) -> bool:
        opened_stat = os.fstat(opened_descriptor)
        if _platform_name() != "posix":  # pragma: no cover - Windows fallback
            try:
                current_stat = os.lstat(directory / name)
            except FileNotFoundError:
                return False
        else:
            directory_descriptor = os.open(
                directory, os.O_RDONLY | _DIRECTORY | _CLOEXEC
            )
            try:
                try:
                    current_stat = os.stat(
                        name, dir_fd=directory_descriptor, follow_symlinks=False
                    )
                except FileNotFoundError:
                    return False
            finally:
                os.close(directory_descriptor)
        return (
            stat.S_ISREG(current_stat.st_mode)
            and current_stat.st_nlink == 1
            and (current_stat.st_dev, current_stat.st_ino)
            == (opened_stat.st_dev, opened_stat.st_ino)
        )

    @staticmethod
    def _unlink_open_name(directory: Path, name: str, opened_descriptor: int) -> bool:
        opened_stat = os.fstat(opened_descriptor)
        if _platform_name() != "posix":  # pragma: no cover - Windows fallback
            try:
                current_stat = os.lstat(directory / name)
            except FileNotFoundError:
                return False
            if (current_stat.st_dev, current_stat.st_ino) != (
                opened_stat.st_dev,
                opened_stat.st_ino,
            ):
                return False
            (directory / name).unlink()
            return True
        directory_descriptor = os.open(
            directory, os.O_RDONLY | _DIRECTORY | _CLOEXEC
        )
        try:
            try:
                current_stat = os.stat(
                    name, dir_fd=directory_descriptor, follow_symlinks=False
                )
            except FileNotFoundError:
                return False
            if (current_stat.st_dev, current_stat.st_ino) != (
                opened_stat.st_dev,
                opened_stat.st_ino,
            ):
                return False
            os.unlink(name, dir_fd=directory_descriptor)
            return True
        finally:
            os.close(directory_descriptor)

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
        if _platform_name() != "posix":  # pragma: no cover - Windows fallback
            return os.open(directory / name, flags, mode)
        descriptor = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | _CLOEXEC)
        try:
            return os.open(name, flags, mode, dir_fd=descriptor)
        finally:
            os.close(descriptor)

    def _publish_noreplace(
        self,
        source_directory: Path,
        source_name: str,
        destination_directory: Path,
        destination_name: str,
    ) -> None:
        if _platform_name() != "posix":  # pragma: no cover - Windows fallback
            os.rename(
                source_directory / source_name,
                destination_directory / destination_name,
            )
            return

        source_fd = os.open(
            source_directory, os.O_RDONLY | _DIRECTORY | _CLOEXEC
        )
        destination_fd = os.open(
            destination_directory, os.O_RDONLY | _DIRECTORY | _CLOEXEC
        )
        try:
            if sys.platform.startswith("linux") and self._linux_rename_noreplace(
                source_fd, source_name, destination_fd, destination_name
            ):
                return
            os.link(
                source_name,
                destination_name,
                src_dir_fd=source_fd,
                dst_dir_fd=destination_fd,
                follow_symlinks=False,
            )
            os.unlink(source_name, dir_fd=source_fd)
        finally:
            os.close(destination_fd)
            os.close(source_fd)

    @staticmethod
    def _linux_rename_noreplace(
        source_fd: int,
        source_name: str,
        destination_fd: int,
        destination_name: str,
    ) -> bool:
        libc = CDLL(None, use_errno=True)
        try:
            renameat2 = libc.renameat2
        except AttributeError:
            return False
        renameat2.argtypes = [c_int, c_char_p, c_int, c_char_p, c_int]
        renameat2.restype = c_int
        result = renameat2(
            source_fd,
            os.fsencode(source_name),
            destination_fd,
            os.fsencode(destination_name),
            _RENAME_NOREPLACE,
        )
        if result == 0:
            return True
        error_number = get_errno()
        if error_number == errno.EEXIST:
            raise FileExistsError(
                error_number, os.strerror(error_number), destination_name
            )
        if error_number in {
            errno.ENOSYS,
            errno.EINVAL,
            errno.ENOTSUP,
            errno.EOPNOTSUPP,
        }:
            return False
        raise OSError(error_number, os.strerror(error_number), source_name)

    @staticmethod
    def _replace_name(
        source_directory: Path, source_name: str, destination_directory: Path, destination_name: str
    ) -> None:
        if _platform_name() != "posix":  # pragma: no cover - Windows fallback
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
        if _platform_name() != "posix":  # pragma: no cover - Windows fallback
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
    def _fchmod_descriptor(descriptor: int, path: Path, mode: int) -> None:
        if hasattr(os, "fchmod"):
            os.fchmod(descriptor, mode)
        else:  # pragma: no cover - Windows fallback
            os.chmod(path, mode)

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
