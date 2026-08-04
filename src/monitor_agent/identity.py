from __future__ import annotations

import hashlib
import os
import re
import subprocess
import sys
import time
from contextlib import AbstractContextManager
from dataclasses import dataclass
from importlib import import_module
from pathlib import Path
from typing import Protocol, cast
from uuid import UUID, uuid4

_MACOS_UUID_PATTERN = re.compile(r'"IOPlatformUUID"\s*=\s*"([^"]+)"')
_FALLBACK_READ_ATTEMPTS = 100
_FALLBACK_READ_DELAY_SEC = 0.01


class _RegistryModule(Protocol):
    HKEY_LOCAL_MACHINE: object

    def OpenKey(self, key: object, sub_key: str) -> AbstractContextManager[object]: ...

    def QueryValueEx(self, key: object, value_name: str) -> tuple[object, int]: ...


@dataclass(frozen=True, slots=True)
class MachineIdentity:
    value: str
    source: str


def _read_linux_machine_id() -> str | None:
    try:
        value = Path("/etc/machine-id").read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return value or None


def _read_windows_machine_id() -> str | None:
    try:
        registry = cast(_RegistryModule, import_module("winreg"))
    except ImportError:
        return None

    try:
        with registry.OpenKey(
            registry.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\Microsoft\Cryptography",
        ) as key:
            value, _ = registry.QueryValueEx(key, "MachineGuid")
    except OSError:
        return None
    if not isinstance(value, str):
        return None
    return value.strip() or None


def _read_macos_machine_id() -> str | None:
    try:
        completed = subprocess.run(
            ["ioreg", "-rd1", "-c", "IOPlatformExpertDevice"],
            capture_output=True,
            check=False,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    match = _MACOS_UUID_PATTERN.search(completed.stdout)
    if match is None:
        return None
    return match.group(1).strip() or None


def _read_fallback_file(path: Path) -> str:
    value = path.read_text(encoding="utf-8").strip()
    parsed = UUID(value)
    if parsed.version != 4:
        raise ValueError("persisted machine identity is not a UUID4")
    return str(parsed)


def _read_concurrent_winner(path: Path) -> str:
    for _ in range(_FALLBACK_READ_ATTEMPTS):
        try:
            value = _read_fallback_file(path)
        except (FileNotFoundError, ValueError):
            time.sleep(_FALLBACK_READ_DELAY_SEC)
        else:
            path.chmod(0o600)
            return value
    raise RuntimeError("concurrent machine identity creation did not complete")


def _load_or_create_fallback(state_dir: Path) -> str:
    state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    state_dir.chmod(0o700)
    path = state_dir / "machine-id"

    try:
        value = _read_fallback_file(path)
    except FileNotFoundError:
        pass
    except ValueError:
        return _read_concurrent_winner(path)
    else:
        path.chmod(0o600)
        return value

    candidate = str(uuid4())
    try:
        file_descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
    except FileExistsError:
        return _read_concurrent_winner(path)

    with os.fdopen(file_descriptor, "w", encoding="utf-8") as stream:
        stream.write(candidate)
        stream.flush()
        os.fsync(stream.fileno())
    path.chmod(0o600)
    return candidate


def _hashed_uuid(raw_identifier: str) -> str:
    digest = bytearray(
        hashlib.sha256(b"monitor-agent/v2\\0" + raw_identifier.encode()).digest()[:16]
    )
    digest[6] = (digest[6] & 0x0F) | 0x50
    digest[8] = (digest[8] & 0x3F) | 0x80
    return str(UUID(bytes=bytes(digest)))


def resolve_machine_identity(
    state_dir: Path,
    *,
    platform_name: str | None = None,
) -> MachineIdentity:
    platform = sys.platform if platform_name is None else platform_name

    if platform == "linux":
        raw_identifier = _read_linux_machine_id()
        if raw_identifier:
            return MachineIdentity(_hashed_uuid(raw_identifier), "linux-machine-id")
    elif platform == "win32":
        raw_identifier = _read_windows_machine_id()
        if raw_identifier:
            return MachineIdentity(_hashed_uuid(raw_identifier), "windows-machine-guid")
    elif platform == "darwin":
        raw_identifier = _read_macos_machine_id()
        if raw_identifier:
            return MachineIdentity(_hashed_uuid(raw_identifier), "macos-platform-uuid")

    return MachineIdentity(_load_or_create_fallback(state_dir), "persisted-random")
