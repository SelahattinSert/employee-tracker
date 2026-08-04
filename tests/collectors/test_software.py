from __future__ import annotations

import json
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier, Lock

import pytest

from monitor_agent.collectors import software as software_module
from monitor_agent.collectors.software import SoftwareCollector
from monitor_agent.models import CollectorStatus

DPKG_COMMAND = ["dpkg-query", "-W", "-f=${Package}\t${Version}\n"]
RPM_COMMAND = ["rpm", "-qa", "--queryformat", "%{NAME}\t%{VERSION}\n"]
BREW_COMMAND = ["brew", "list", "--versions"]
WINDOWS_PATHS = [
    (
        "HKLM",
        r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
    ),
    (
        "HKLM",
        r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall",
    ),
    (
        "HKCU",
        r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
    ),
]


def test_linux_software_results_are_cached_and_mutation_resistant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[list[str], float]] = []

    def fake_run(command: list[str], *, timeout: float) -> str:
        calls.append((command, timeout))
        return "alpha\t1.0\nbeta\t2.0\n"

    monkeypatch.setattr(software_module, "_run_command", fake_run)
    monkeypatch.setattr(software_module.sys, "platform", "linux")
    collector = SoftwareCollector(cache_ttl_sec=86400)

    first = collector.collect()
    first.data["software"][0]["name"] = "mutated"
    second = collector.collect()

    assert second.status is CollectorStatus.SUCCESS
    assert second.data == {
        "software": [
            {"name": "alpha", "version": "1.0", "source": "dpkg"},
            {"name": "beta", "version": "2.0", "source": "dpkg"},
        ]
    }
    assert calls == [(DPKG_COMMAND, 20.0)]
    json.dumps(second.data, allow_nan=False)


def test_cache_expires_using_monotonic_time(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0
    now = [100.0]

    def fake_run(command: list[str], *, timeout: float) -> str:
        nonlocal calls
        calls += 1
        return f"alpha\t{calls}.0\n"

    monkeypatch.setattr(software_module, "_run_command", fake_run)
    monkeypatch.setattr(software_module.sys, "platform", "linux")
    monkeypatch.setattr(software_module.time, "monotonic", lambda: now[0])
    collector = SoftwareCollector(cache_ttl_sec=10)

    assert collector.collect().data["software"][0]["version"] == "1.0"
    now[0] = 109.999
    assert collector.collect().data["software"][0]["version"] == "1.0"
    now[0] = 110.0
    assert collector.collect().data["software"][0]["version"] == "2.0"
    now[0] = 90.0
    assert collector.collect().data["software"][0]["version"] == "3.0"
    assert calls == 3


def test_cache_ttl_starts_when_refresh_completes(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0
    now = [100.0]

    def fake_run(command: list[str], *, timeout: float) -> str:
        nonlocal calls
        calls += 1
        now[0] = 115.0
        return "alpha\t1.0\n"

    monkeypatch.setattr(software_module, "_run_command", fake_run)
    monkeypatch.setattr(software_module.sys, "platform", "linux")
    monkeypatch.setattr(software_module.time, "monotonic", lambda: now[0])
    collector = SoftwareCollector(cache_ttl_sec=10)

    collector.collect()
    now[0] = 116.0
    collector.collect()

    assert calls == 1


def test_cache_is_per_instance(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0

    def fake_run(command: list[str], *, timeout: float) -> str:
        nonlocal calls
        calls += 1
        return "alpha\t1.0\n"

    monkeypatch.setattr(software_module, "_run_command", fake_run)
    monkeypatch.setattr(software_module.sys, "platform", "linux")

    SoftwareCollector().collect()
    SoftwareCollector().collect()

    assert calls == 2


def test_concurrent_collects_share_one_locked_refresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0
    calls_lock = Lock()
    start = Barrier(3)

    def fake_run(command: list[str], *, timeout: float) -> str:
        nonlocal calls
        with calls_lock:
            calls += 1
        return "alpha\t1.0\n"

    monkeypatch.setattr(software_module, "_run_command", fake_run)
    monkeypatch.setattr(software_module.sys, "platform", "linux")
    collector = SoftwareCollector()

    def collect_after_barrier() -> object:
        start.wait()
        return collector.collect().data

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(collect_after_barrier) for _ in range(2)]
        start.wait()
        results = [future.result(timeout=1) for future in futures]

    assert results[0] == results[1]
    assert calls == 1


@pytest.mark.parametrize("cache_ttl_sec", [-0.1, float("nan"), float("inf")])
def test_invalid_cache_ttl_is_rejected(cache_ttl_sec: float) -> None:
    with pytest.raises(ValueError, match="finite and non-negative"):
        SoftwareCollector(cache_ttl_sec=cache_ttl_sec)


def test_zero_cache_ttl_disables_reuse(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0

    def fake_run(command: list[str], *, timeout: float) -> str:
        nonlocal calls
        calls += 1
        return "alpha\t1.0\n"

    monkeypatch.setattr(software_module, "_run_command", fake_run)
    monkeypatch.setattr(software_module.sys, "platform", "linux")
    collector = SoftwareCollector(cache_ttl_sec=0)

    collector.collect()
    collector.collect()

    assert calls == 2


def test_disabled_software_collector_executes_no_platform_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def executed(*args: object, **kwargs: object) -> object:
        raise AssertionError("executed")

    monkeypatch.setattr(software_module, "_run_command", executed)
    monkeypatch.setattr(software_module, "_load_winreg", executed)
    monkeypatch.setattr(software_module.Path, "glob", executed)

    result = SoftwareCollector(enabled=False).collect()

    assert result.status is CollectorStatus.DISABLED
    assert result.data == {"software": []}


def test_linux_uses_exact_rpm_fallback_command_and_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[list[str], float]] = []

    def fake_run(command: list[str], *, timeout: float) -> str:
        calls.append((command, timeout))
        if command == DPKG_COMMAND:
            raise FileNotFoundError("private/dpkg-query")
        return "zeta\t9\nAlpha\t1\n"

    monkeypatch.setattr(software_module, "_run_command", fake_run)
    monkeypatch.setattr(software_module.sys, "platform", "linux")

    result = SoftwareCollector().collect()

    assert result.status is CollectorStatus.SUCCESS
    assert result.data == {
        "software": [
            {"name": "Alpha", "version": "1", "source": "rpm"},
            {"name": "zeta", "version": "9", "source": "rpm"},
        ]
    }
    assert calls == [(DPKG_COMMAND, 20.0), (RPM_COMMAND, 20.0)]
    assert "private" not in repr(result)


def test_successful_empty_dpkg_output_does_not_execute_rpm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def fake_run(command: list[str], *, timeout: float) -> str:
        calls.append(command)
        return "\n\t\nmalformed\n"

    monkeypatch.setattr(software_module, "_run_command", fake_run)
    monkeypatch.setattr(software_module.sys, "platform", "linux")

    result = SoftwareCollector().collect()

    assert result.status is CollectorStatus.SUCCESS
    assert result.data == {"software": []}
    assert calls == [DPKG_COMMAND]


def test_linux_parsing_deduplicates_and_sorts_casefolded_records(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(software_module.sys, "platform", "linux")
    monkeypatch.setattr(
        software_module,
        "_run_command",
        lambda command, timeout: (
            "zeta\t2\n"
            "Alpha\t1\n"
            "alpha\t1\n"
            "beta\t\n"
            "missing-tab\n"
            "\tmissing-name\n"
            "too\tmany\tcolumns\n"
            "\n"
        ),
    )

    result = SoftwareCollector().collect()

    assert result.data == {
        "software": [
            {"name": "Alpha", "version": "1", "source": "dpkg"},
            {"name": "beta", "version": None, "source": "dpkg"},
            {"name": "zeta", "version": "2", "source": "dpkg"},
        ]
    }


def test_all_linux_managers_unavailable_returns_fixed_sanitized_partial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(command: list[str], *, timeout: float) -> str:
        if command == DPKG_COMMAND:
            raise subprocess.TimeoutExpired(command, timeout, stderr="private-secret")
        raise subprocess.CalledProcessError(2, command, stderr="private-secret")

    monkeypatch.setattr(software_module, "_run_command", fail)
    monkeypatch.setattr(software_module.sys, "platform", "linux")

    result = SoftwareCollector().collect()

    assert result.status is CollectorStatus.PARTIAL
    assert result.error_code == "software_inventory_unavailable"
    assert result.error_message == "software inventory unavailable"
    assert result.data == {"software": []}
    assert "private-secret" not in repr(result)
    assert "dpkg-query" not in repr(result)
    assert "rpm" not in repr(result)


class FakeRegistryKey:
    def __init__(
        self,
        root: tuple[str, str],
        subkey: str | None = None,
    ) -> None:
        self.root = root
        self.subkey = subkey


class FakeWinReg:
    HKEY_LOCAL_MACHINE = "HKLM"
    HKEY_CURRENT_USER = "HKCU"

    def __init__(
        self,
        entries: dict[tuple[str, str], list[dict[str, object]]],
        *,
        failed_roots: set[tuple[str, str]] | None = None,
    ) -> None:
        self.entries = entries
        self.failed_roots = failed_roots or set()
        self.root_calls: list[tuple[object, str]] = []
        self.opened: list[FakeRegistryKey] = []
        self.closed: list[FakeRegistryKey] = []

    def OpenKey(self, key: object, path: str) -> FakeRegistryKey:
        if isinstance(key, FakeRegistryKey):
            if path == "broken-subkey":
                raise OSError("private registry subkey")
            opened = FakeRegistryKey(key.root, path)
        else:
            self.root_calls.append((key, path))
            root = (str(key), path)
            if root in self.failed_roots:
                raise PermissionError("private registry path")
            opened = FakeRegistryKey(root)
        self.opened.append(opened)
        return opened

    def QueryInfoKey(self, key: FakeRegistryKey) -> tuple[int, int, int]:
        return (len(self.entries.get(key.root, [])), 0, 0)

    def EnumKey(self, key: FakeRegistryKey, index: int) -> str:
        entry = self.entries[key.root][index]
        return str(entry.get("key", index))

    def QueryValueEx(
        self,
        key: FakeRegistryKey,
        name: str,
    ) -> tuple[object, int]:
        for entry in self.entries[key.root]:
            if str(entry.get("key")) == key.subkey:
                if name not in entry:
                    raise OSError("private missing value")
                return entry[name], 1
        raise OSError("private missing subkey")

    def CloseKey(self, key: FakeRegistryKey) -> None:
        self.closed.append(key)


def test_windows_enumerates_exact_legacy_roots_and_closes_every_opened_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = FakeWinReg(
        {
            WINDOWS_PATHS[0]: [
                {"key": "alpha", "DisplayName": "Alpha", "DisplayVersion": "1"},
                {"key": "broken-subkey"},
                {"key": "blank", "DisplayName": "   ", "DisplayVersion": "9"},
            ],
            WINDOWS_PATHS[1]: [{"key": "duplicate", "DisplayName": "alpha", "DisplayVersion": "1"}],
            WINDOWS_PATHS[2]: [
                {"key": "beta", "DisplayName": "Beta"},
                {"key": "unsafe", "DisplayName": object()},
            ],
        }
    )
    monkeypatch.setattr(software_module, "_load_winreg", lambda: registry)
    monkeypatch.setattr(software_module.sys, "platform", "win32")

    result = SoftwareCollector().collect()

    assert result.status is CollectorStatus.SUCCESS
    assert result.data == {
        "software": [
            {
                "name": "Alpha",
                "version": "1",
                "source": "windows_registry",
            },
            {
                "name": "Beta",
                "version": None,
                "source": "windows_registry",
            },
        ]
    }
    assert registry.root_calls == WINDOWS_PATHS
    assert registry.closed == list(reversed(registry.opened)) or set(
        map(id, registry.closed)
    ) == set(map(id, registry.opened))
    assert len(registry.closed) == len(registry.opened)
    assert "private" not in repr(result)


def test_windows_succeeds_when_one_root_opens_and_others_fail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = FakeWinReg(
        {WINDOWS_PATHS[2]: []},
        failed_roots={WINDOWS_PATHS[0], WINDOWS_PATHS[1]},
    )
    monkeypatch.setattr(software_module, "_load_winreg", lambda: registry)
    monkeypatch.setattr(software_module.sys, "platform", "win32")

    result = SoftwareCollector().collect()

    assert result.status is CollectorStatus.SUCCESS
    assert result.data == {"software": []}
    assert len(registry.opened) == 1
    assert registry.closed == registry.opened


def test_windows_all_roots_unavailable_is_partial(monkeypatch: pytest.MonkeyPatch) -> None:
    registry = FakeWinReg(
        {},
        failed_roots=set(WINDOWS_PATHS),
    )
    monkeypatch.setattr(software_module, "_load_winreg", lambda: registry)
    monkeypatch.setattr(software_module.sys, "platform", "win32")

    result = SoftwareCollector().collect()

    assert result.status is CollectorStatus.PARTIAL
    assert result.error_code == "software_inventory_unavailable"
    assert result.error_message == "software inventory unavailable"
    assert result.data == {"software": []}
    assert "SOFTWARE" not in repr(result)


def test_macos_combines_applications_and_brew_with_exact_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    glob_calls: list[tuple[str, str]] = []
    command_calls: list[tuple[list[str], float]] = []

    def fake_glob(path: Path, pattern: str) -> list[Path]:
        glob_calls.append((str(path), pattern))
        return [Path("/Applications/Zeta.app"), Path("/Applications/alpha.app")]

    def fake_run(command: list[str], *, timeout: float) -> str:
        command_calls.append((command, timeout))
        return "beta 2.0 2.1\nAlpha 1.0\nblank-version\n\n"

    monkeypatch.setattr(software_module.Path, "glob", fake_glob)
    monkeypatch.setattr(software_module, "_run_command", fake_run)
    monkeypatch.setattr(software_module.sys, "platform", "darwin")

    result = SoftwareCollector().collect()

    assert result.status is CollectorStatus.SUCCESS
    assert result.data == {
        "software": [
            {
                "name": "alpha",
                "version": None,
                "source": "macos_applications",
            },
            {"name": "Alpha", "version": "1.0", "source": "homebrew"},
            {"name": "beta", "version": "2.0 2.1", "source": "homebrew"},
            {"name": "blank-version", "version": None, "source": "homebrew"},
            {
                "name": "Zeta",
                "version": None,
                "source": "macos_applications",
            },
        ]
    }
    assert glob_calls == [("/Applications", "*.app")]
    assert command_calls == [(BREW_COMMAND, 20.0)]


def test_macos_one_available_source_is_success(monkeypatch: pytest.MonkeyPatch) -> None:
    def failed_glob(path: Path, pattern: str) -> list[Path]:
        raise PermissionError("private /Applications path")

    monkeypatch.setattr(software_module.Path, "glob", failed_glob)
    monkeypatch.setattr(
        software_module,
        "_run_command",
        lambda command, timeout: "brew-package 1.0\n",
    )
    monkeypatch.setattr(software_module.sys, "platform", "darwin")

    result = SoftwareCollector().collect()

    assert result.status is CollectorStatus.SUCCESS
    assert result.data == {
        "software": [{"name": "brew-package", "version": "1.0", "source": "homebrew"}]
    }
    assert "private" not in repr(result)


def test_macos_all_sources_fail_with_no_raw_error_leak(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def failed_glob(path: Path, pattern: str) -> list[Path]:
        raise PermissionError("private /Applications path token=secret")

    def failed_brew(command: list[str], *, timeout: float) -> str:
        raise FileNotFoundError("private /opt/homebrew/bin/brew token=secret")

    monkeypatch.setattr(software_module.Path, "glob", failed_glob)
    monkeypatch.setattr(software_module, "_run_command", failed_brew)
    monkeypatch.setattr(software_module.sys, "platform", "darwin")

    result = SoftwareCollector().collect()

    assert result.status is CollectorStatus.PARTIAL
    assert result.error_code == "software_inventory_unavailable"
    assert result.error_message == "software inventory unavailable"
    assert result.data == {"software": []}
    assert "private" not in repr(result)
    assert "secret" not in repr(result)
    assert "Applications" not in repr(result)
    assert "brew" not in repr(result)


def test_unsupported_platform_is_fixed_partial(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(software_module.sys, "platform", "freebsd14")

    result = SoftwareCollector().collect()

    assert result.status is CollectorStatus.PARTIAL
    assert result.error_code == "software_inventory_unavailable"
    assert result.error_message == "software inventory unavailable"
    assert result.data == {"software": []}
