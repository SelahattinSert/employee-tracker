from __future__ import annotations

import importlib
import math
import subprocess
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Protocol, cast

from monitor_agent.models import CollectorPayload, CollectorStatus, JSONValue

_COMMAND_TIMEOUT_SEC = 20.0
_DPKG_COMMAND = ["dpkg-query", "-W", "-f=${Package}\t${Version}\n"]
_RPM_COMMAND = ["rpm", "-qa", "--queryformat", "%{NAME}\t%{VERSION}\n"]
_BREW_COMMAND = ["brew", "list", "--versions"]
_WINDOWS_UNINSTALL_PATHS = (
    r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
    r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall",
    r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
)


class _WinReg(Protocol):
    HKEY_LOCAL_MACHINE: object
    HKEY_CURRENT_USER: object

    def OpenKey(self, key: object, sub_key: str) -> object: ...

    def QueryInfoKey(self, key: object) -> tuple[int, int, int]: ...

    def EnumKey(self, key: object, index: int) -> str: ...

    def QueryValueEx(self, key: object, value_name: str) -> tuple[object, int]: ...

    def CloseKey(self, key: object) -> None: ...


@dataclass(frozen=True, slots=True)
class _SoftwareRecord:
    name: str
    version: str | None
    source: str


def _run_command(command: Sequence[str], *, timeout: float) -> str:
    return subprocess.check_output(
        list(command),
        stderr=subprocess.DEVNULL,
        text=True,
        timeout=timeout,
    )


def _load_winreg() -> _WinReg:
    return cast(_WinReg, importlib.import_module("winreg"))


def _clean_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def _parse_tab_records(output: str, source: str) -> list[_SoftwareRecord]:
    records: list[_SoftwareRecord] = []
    for line in output.splitlines():
        parts = line.split("\t")
        if len(parts) != 2:
            continue
        name = _clean_text(parts[0])
        if name is None:
            continue
        records.append(_SoftwareRecord(name, _clean_text(parts[1]), source))
    return records


def _parse_brew_records(output: str) -> list[_SoftwareRecord]:
    records: list[_SoftwareRecord] = []
    for line in output.splitlines():
        parts = line.split(maxsplit=1)
        if not parts:
            continue
        name = _clean_text(parts[0])
        if name is None:
            continue
        version = _clean_text(parts[1]) if len(parts) == 2 else None
        records.append(_SoftwareRecord(name, version, "homebrew"))
    return records


def _normalize_records(records: Sequence[_SoftwareRecord]) -> tuple[_SoftwareRecord, ...]:
    unique: dict[tuple[str, str, str], _SoftwareRecord] = {}
    for record in records:
        key = (
            record.name.casefold(),
            (record.version or "").casefold(),
            record.source.casefold(),
        )
        unique.setdefault(key, record)
    return tuple(unique[key] for key in sorted(unique))


def _collect_linux() -> tuple[list[_SoftwareRecord], bool]:
    for command, source in ((_DPKG_COMMAND, "dpkg"), (_RPM_COMMAND, "rpm")):
        try:
            output = _run_command(command, timeout=_COMMAND_TIMEOUT_SEC)
        except Exception:
            continue
        return _parse_tab_records(output, source), True
    return [], False


def _close_registry_key(registry: _WinReg, key: object | None) -> None:
    if key is None:
        return
    try:
        registry.CloseKey(key)
    except Exception:
        return


def _registry_text(registry: _WinReg, key: object, name: str) -> str | None:
    try:
        value = registry.QueryValueEx(key, name)[0]
    except Exception:
        return None
    return _clean_text(value)


def _collect_windows() -> tuple[list[_SoftwareRecord], bool]:
    try:
        registry = _load_winreg()
    except Exception:
        return [], False

    roots = (
        (registry.HKEY_LOCAL_MACHINE, _WINDOWS_UNINSTALL_PATHS[0]),
        (registry.HKEY_LOCAL_MACHINE, _WINDOWS_UNINSTALL_PATHS[1]),
        (registry.HKEY_CURRENT_USER, _WINDOWS_UNINSTALL_PATHS[2]),
    )
    records: list[_SoftwareRecord] = []
    available = False
    for hive, path in roots:
        root_key: object | None = None
        try:
            root_key = registry.OpenKey(hive, path)
            subkey_count = registry.QueryInfoKey(root_key)[0]
            if not isinstance(subkey_count, int) or isinstance(subkey_count, bool):
                continue
            available = True
            for index in range(max(0, subkey_count)):
                subkey: object | None = None
                try:
                    subkey_name = registry.EnumKey(root_key, index)
                    subkey = registry.OpenKey(root_key, subkey_name)
                    display_name = _registry_text(registry, subkey, "DisplayName")
                    if display_name is None:
                        continue
                    records.append(
                        _SoftwareRecord(
                            display_name,
                            _registry_text(registry, subkey, "DisplayVersion"),
                            "windows_registry",
                        )
                    )
                except Exception:
                    continue
                finally:
                    _close_registry_key(registry, subkey)
        except Exception:
            continue
        finally:
            _close_registry_key(registry, root_key)
    return records, available


def _collect_macos() -> tuple[list[_SoftwareRecord], bool]:
    records: list[_SoftwareRecord] = []
    available = False
    try:
        application_records = [
            _SoftwareRecord(path.stem, None, "macos_applications")
            for path in Path("/Applications").glob("*.app")
            if _clean_text(path.stem) is not None
        ]
    except Exception:
        pass
    else:
        records.extend(application_records)
        available = True

    try:
        brew_output = _run_command(_BREW_COMMAND, timeout=_COMMAND_TIMEOUT_SEC)
    except Exception:
        pass
    else:
        records.extend(_parse_brew_records(brew_output))
        available = True
    return records, available


def _payload(records: Sequence[_SoftwareRecord]) -> CollectorPayload:
    software: list[JSONValue] = [
        {
            "name": record.name,
            "version": record.version,
            "source": record.source,
        }
        for record in records
    ]
    return CollectorPayload(data={"software": software})


class SoftwareCollector:
    name = "software"

    def __init__(self, enabled: bool = True, cache_ttl_sec: float = 86400.0) -> None:
        if not math.isfinite(cache_ttl_sec) or cache_ttl_sec < 0:
            raise ValueError("cache_ttl_sec must be finite and non-negative")
        self._enabled = enabled
        self._cache_ttl_sec = float(cache_ttl_sec)
        self._cache: tuple[float, tuple[_SoftwareRecord, ...]] | None = None
        self._lock = Lock()

    def collect(self) -> CollectorPayload:
        if not self._enabled:
            return CollectorPayload(
                data={"software": []},
                status=CollectorStatus.DISABLED,
            )

        with self._lock:
            now = time.monotonic()
            if self._cache is not None:
                cached_at, cached_records = self._cache
                age = now - cached_at
                if 0 <= age < self._cache_ttl_sec:
                    return _payload(cached_records)

            if sys.platform == "linux":
                records, available = _collect_linux()
            elif sys.platform == "win32":
                records, available = _collect_windows()
            elif sys.platform == "darwin":
                records, available = _collect_macos()
            else:
                records, available = [], False

            if not available:
                return CollectorPayload(
                    data={"software": []},
                    status=CollectorStatus.PARTIAL,
                    error_code="software_inventory_unavailable",
                    error_message="software inventory unavailable",
                )

            immutable_records = _normalize_records(records)
            self._cache = (time.monotonic(), immutable_records)
            return _payload(immutable_records)
