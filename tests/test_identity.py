from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, fields
from pathlib import Path
from threading import Barrier, Event
from types import ModuleType
from uuid import UUID

import pytest

import monitor_agent.identity as identity_module
from monitor_agent.identity import (
    MachineIdentity,
    _hashed_uuid,
    _load_or_create_fallback,
    _read_linux_machine_id,
    _read_macos_machine_id,
    _read_windows_machine_id,
    resolve_machine_identity,
)

UUID_PATTERN = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def test_machine_identity_is_frozen_slotted_and_has_exact_fields() -> None:
    machine_identity = MachineIdentity("private-id", "test-source")

    assert [field.name for field in fields(MachineIdentity)] == ["value", "source"]
    assert not hasattr(machine_identity, "__dict__")
    with pytest.raises(FrozenInstanceError):
        machine_identity.value = "replacement"


def test_hashed_uuid_matches_domain_separated_golden_vector() -> None:
    raw_identifier = "raw-platform-machine-id"

    value = _hashed_uuid(raw_identifier)

    assert value == "5573d7a7-80aa-591f-b5c9-0045f85c134e"
    assert UUID(value).version == 5
    assert UUID(value).variant == "specified in RFC 4122"
    assert raw_identifier not in value


def test_linux_machine_id_is_hashed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        "monitor_agent.identity._read_linux_machine_id",
        lambda: "raw-platform-machine-id",
    )

    identity = resolve_machine_identity(tmp_path, platform_name="linux")

    assert identity == resolve_machine_identity(tmp_path, platform_name="linux")
    assert identity.source == "linux-machine-id"
    assert identity.value != "raw-platform-machine-id"
    assert UUID_PATTERN.fullmatch(identity.value)


def test_windows_machine_guid_is_hashed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        "monitor_agent.identity._read_windows_machine_id",
        lambda: "raw-windows-machine-guid",
    )

    identity = resolve_machine_identity(tmp_path, platform_name="win32")

    assert identity.source == "windows-machine-guid"
    assert identity.value == _hashed_uuid("raw-windows-machine-guid")


def test_hash_never_contains_raw_macos_identifier(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        "monitor_agent.identity._read_macos_machine_id",
        lambda: "IOPlatformUUID-secret",
    )

    identity = resolve_machine_identity(tmp_path, platform_name="darwin")

    assert identity.source == "macos-platform-uuid"
    assert "secret" not in identity.value


def test_platform_defaults_to_sys_platform(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(identity_module.sys, "platform", "darwin")
    monkeypatch.setattr(
        identity_module,
        "_read_macos_machine_id",
        lambda: "raw-macos-identifier",
    )

    identity = resolve_machine_identity(tmp_path)

    assert identity == MachineIdentity(_hashed_uuid("raw-macos-identifier"), "macos-platform-uuid")


def test_missing_platform_id_persists_random_fallback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("monitor_agent.identity._read_linux_machine_id", lambda: None)

    first = resolve_machine_identity(tmp_path, platform_name="linux")
    second = resolve_machine_identity(tmp_path, platform_name="linux")

    assert first == second
    assert first.source == "persisted-random"
    assert UUID(first.value).version == 4
    assert (tmp_path / "machine-id").stat().st_mode & 0o777 == 0o600


def test_empty_platform_identifier_uses_fallback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("monitor_agent.identity._read_linux_machine_id", lambda: "")

    identity = resolve_machine_identity(tmp_path, platform_name="linux")

    assert identity.source == "persisted-random"
    assert UUID(identity.value).version == 4


def test_unknown_platform_uses_existing_fallback(tmp_path: Path) -> None:
    expected = "12345678-1234-4678-9234-567812345678"
    (tmp_path / "machine-id").write_text(f" {expected}\n", encoding="utf-8")

    identity = resolve_machine_identity(tmp_path, platform_name="unsupported")

    assert identity == MachineIdentity(expected, "persisted-random")


def test_linux_reader_strips_machine_id(monkeypatch: pytest.MonkeyPatch) -> None:
    def read_text(path: Path, *, encoding: str) -> str:
        assert path == Path("/etc/machine-id")
        assert encoding == "utf-8"
        return "  linux-machine-id\n"

    monkeypatch.setattr(Path, "read_text", read_text)

    assert _read_linux_machine_id() == "linux-machine-id"


@pytest.mark.parametrize("result", [" \n", OSError("machine id unavailable")])
def test_linux_reader_rejects_missing_or_empty_values(
    monkeypatch: pytest.MonkeyPatch, result: str | OSError
) -> None:
    def read_text(path: Path, *, encoding: str) -> str:
        if isinstance(result, OSError):
            raise result
        return result

    monkeypatch.setattr(Path, "read_text", read_text)

    assert _read_linux_machine_id() is None


class _RegistryKey:
    def __init__(self) -> None:
        self.closed = False

    def __enter__(self) -> _RegistryKey:
        return self

    def __exit__(self, *args: object) -> None:
        self.closed = True


def _install_fake_winreg(
    monkeypatch: pytest.MonkeyPatch,
    *,
    value: object = " windows-machine-guid ",
    open_error: OSError | None = None,
) -> tuple[ModuleType, _RegistryKey]:
    registry = ModuleType("winreg")
    root = object()
    key = _RegistryKey()
    registry.HKEY_LOCAL_MACHINE = root

    def open_key(received_root: object, path: str) -> _RegistryKey:
        assert received_root is root
        assert path == r"SOFTWARE\Microsoft\Cryptography"
        if open_error is not None:
            raise open_error
        return key

    def query_value(received_key: object, name: str) -> tuple[object, int]:
        assert received_key is key
        assert name == "MachineGuid"
        return value, 1

    registry.OpenKey = open_key
    registry.QueryValueEx = query_value
    monkeypatch.setitem(sys.modules, "winreg", registry)
    return registry, key


def test_windows_reader_reads_machine_guid_and_closes_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, key = _install_fake_winreg(monkeypatch)

    assert _read_windows_machine_id() == "windows-machine-guid"
    assert key.closed is True


@pytest.mark.parametrize("value", ["  ", 42])
def test_windows_reader_rejects_blank_or_non_string_values(
    monkeypatch: pytest.MonkeyPatch, value: object
) -> None:
    _install_fake_winreg(monkeypatch, value=value)

    assert _read_windows_machine_id() is None


def test_windows_reader_handles_registry_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_winreg(monkeypatch, open_error=OSError("registry unavailable"))

    assert _read_windows_machine_id() is None


def test_windows_reader_handles_missing_winreg(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        identity_module,
        "import_module",
        lambda name: (_ for _ in ()).throw(ImportError("winreg unavailable")),
    )

    assert _read_windows_machine_id() is None


def test_macos_reader_runs_ioreg_with_timeout_and_extracts_uuid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[list[str], dict[str, object]]] = []

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(
            command,
            0,
            stdout='    "IOPlatformUUID" = " macos-platform-uuid "\n',
            stderr="",
        )

    monkeypatch.setattr(identity_module.subprocess, "run", run)

    assert _read_macos_machine_id() == "macos-platform-uuid"
    assert calls == [
        (
            ["ioreg", "-rd1", "-c", "IOPlatformExpertDevice"],
            {"capture_output": True, "check": False, "text": True, "timeout": 5},
        )
    ]


@pytest.mark.parametrize(
    "outcome",
    [
        subprocess.CompletedProcess(["ioreg"], 1, stdout="", stderr="failed"),
        subprocess.CompletedProcess(["ioreg"], 0, stdout="no platform uuid", stderr=""),
        subprocess.TimeoutExpired(["ioreg"], 5),
        OSError("ioreg unavailable"),
    ],
)
def test_macos_reader_rejects_failed_or_missing_output(
    monkeypatch: pytest.MonkeyPatch,
    outcome: subprocess.CompletedProcess[str] | subprocess.TimeoutExpired | OSError,
) -> None:
    def run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(identity_module.subprocess, "run", run)

    assert _read_macos_machine_id() is None


def test_new_fallback_uses_exclusive_owner_only_file_and_fsync(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    state_dir = tmp_path / "state"
    real_open = os.open
    real_fsync = os.fsync
    open_calls: list[tuple[Path, int, int]] = []
    fsync_calls: list[int] = []

    def tracked_open(path: os.PathLike[str], flags: int, mode: int = 0o777) -> int:
        open_calls.append((Path(path), flags, mode))
        return real_open(path, flags, mode)

    def tracked_fsync(file_descriptor: int) -> None:
        fsync_calls.append(file_descriptor)
        real_fsync(file_descriptor)

    monkeypatch.setattr(identity_module.os, "open", tracked_open)
    monkeypatch.setattr(identity_module.os, "fsync", tracked_fsync)

    value = _load_or_create_fallback(state_dir)

    fallback_path = state_dir / "machine-id"
    assert UUID(value).version == 4
    assert fallback_path.read_text(encoding="utf-8").strip() == value
    assert open_calls[0][0] == fallback_path
    assert open_calls[0][1] & (os.O_CREAT | os.O_EXCL) == os.O_CREAT | os.O_EXCL
    assert open_calls[0][2] == 0o600
    assert fsync_calls
    assert state_dir.stat().st_mode & 0o777 == 0o700
    assert fallback_path.stat().st_mode & 0o777 == 0o600


def test_existing_fallback_is_stripped_and_permissions_are_restricted(tmp_path: Path) -> None:
    fallback_path = tmp_path / "machine-id"
    expected = "12345678-1234-4678-9234-567812345678"
    fallback_path.write_text(f" {expected}\n", encoding="utf-8")
    fallback_path.chmod(0o644)

    value = _load_or_create_fallback(tmp_path)

    assert value == expected
    assert fallback_path.stat().st_mode & 0o777 == 0o600


def test_concurrent_fallback_creators_return_the_winners_value(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    state_dir = tmp_path / "racing-state"
    barrier = Barrier(2)
    real_open = os.open

    def contended_open(path: os.PathLike[str], flags: int, mode: int = 0o777) -> int:
        barrier.wait(timeout=2)
        file_descriptor = real_open(path, flags, mode)
        time.sleep(0.05)
        return file_descriptor

    monkeypatch.setattr(identity_module.os, "open", contended_open)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(_load_or_create_fallback, state_dir) for _ in range(2)]
        values = [future.result(timeout=3) for future in futures]

    assert values[0] == values[1]
    assert UUID(values[0]).version == 4
    assert (state_dir / "machine-id").read_text(encoding="utf-8").strip() == values[0]


def test_late_reader_waits_for_empty_concurrent_winner(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    state_dir = tmp_path / "late-reader-state"
    empty_file_created = Event()
    reader_retrying = Event()
    allow_writer = Event()
    real_open = os.open

    def gated_open(path: os.PathLike[str], flags: int, mode: int = 0o777) -> int:
        file_descriptor = real_open(path, flags, mode)
        empty_file_created.set()
        if not allow_writer.wait(timeout=2):
            raise TimeoutError("writer gate was not released")
        return file_descriptor

    def gated_retry(delay: float) -> None:
        assert delay == identity_module._FALLBACK_READ_DELAY_SEC
        reader_retrying.set()
        if not allow_writer.wait(timeout=2):
            raise TimeoutError("reader retry gate was not released")

    monkeypatch.setattr(identity_module.os, "open", gated_open)
    monkeypatch.setattr(identity_module.time, "sleep", gated_retry)

    with ThreadPoolExecutor(max_workers=2) as executor:
        writer = executor.submit(_load_or_create_fallback, state_dir)
        assert empty_file_created.wait(timeout=2)
        reader = executor.submit(_load_or_create_fallback, state_dir)
        try:
            assert reader_retrying.wait(timeout=1)
        finally:
            allow_writer.set()
        writer_value = writer.result(timeout=2)
        reader_value = reader.result(timeout=2)

    assert reader_value == writer_value
    assert UUID(writer_value).version == 4


def test_permanently_malformed_fallback_fails_after_bounded_retries(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fallback_path = tmp_path / "machine-id"
    fallback_path.write_text("not-a-uuid", encoding="utf-8")
    retry_delays: list[float] = []
    monkeypatch.setattr(identity_module, "_FALLBACK_READ_ATTEMPTS", 3)
    monkeypatch.setattr(identity_module.time, "sleep", retry_delays.append)

    with pytest.raises(RuntimeError, match="concurrent machine identity creation did not complete"):
        _load_or_create_fallback(tmp_path)

    assert retry_delays == [identity_module._FALLBACK_READ_DELAY_SEC] * 3
