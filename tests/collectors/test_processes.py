from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace

import psutil
import pytest

from monitor_agent.collectors import processes as processes_module
from monitor_agent.collectors.processes import ProcessesCollector, redact_command_line
from monitor_agent.models import CollectorStatus


class FakeProcess:
    def __init__(
        self,
        info: dict[str, object] | None = None,
        error: Exception | None = None,
    ) -> None:
        self._info = info or {}
        self._error = error

    @property
    def info(self) -> dict[str, object]:
        if self._error is not None:
            raise self._error
        return self._info


def process_info(
    pid: int,
    rss: int,
    cmdline: object,
    **overrides: object,
) -> dict[str, object]:
    info: dict[str, object] = {
        "pid": pid,
        "name": f"process-{pid}",
        "username": "employee",
        "status": "running",
        "cpu_percent": 4.5,
        "memory_info": SimpleNamespace(rss=rss),
        "exe": f"/usr/bin/process-{pid}",
        "cmdline": cmdline,
        "create_time": 1_700_000_000.0,
    }
    info.update(overrides)
    return info


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        (["worker", "--token", "abc123", "--port", "443"], "worker --token *** --port 443"),
        (["worker", "--password=hunter2"], "worker '--password=***'"),
        (["worker", "API_KEY=secret", "MODE=prod"], "worker 'API_KEY=***' MODE=prod"),
        (["worker", "--authorization", "Bearer secret"], "worker --authorization ***"),
        (["worker", "--ToKeN", "mixed-case-secret"], "worker --ToKeN ***"),
        (["worker", "--PASSWORD=mixed-case-secret"], "worker '--PASSWORD=***'"),
        (["worker", "clientToken=secret=value"], "worker 'clientToken=***'"),
        (["worker", "--config-auth=secret"], "worker '--config-auth=***'"),
        (["worker", "--token"], "worker --token"),
    ],
)
def test_redacted_mode_masks_secret_values(
    arguments: list[str], expected: str
) -> None:
    rendered = redact_command_line(arguments, "redacted", platform_name="linux")

    assert rendered == expected
    for secret in ("abc123", "hunter2", "Bearer secret", "mixed-case-secret", "secret=value"):
        if secret in " ".join(arguments):
            assert secret not in rendered


def test_none_mode_returns_empty_string() -> None:
    assert redact_command_line(["worker", "--token", "secret"], "none") == ""


def test_full_mode_preserves_arguments_with_posix_quoting() -> None:
    assert (
        redact_command_line(
            ["worker app", "--port", "443"],
            "full",
            platform_name="linux",
        )
        == "'worker app' --port 443"
    )


def test_windows_uses_list2cmdline_after_redaction() -> None:
    arguments = ["worker app", "--token", "do-not-print", "--port", "443"]

    rendered = redact_command_line(arguments, "redacted", platform_name="win32")

    assert rendered == subprocess.list2cmdline(
        ["worker app", "--token", "***", "--port", "443"]
    )
    assert "do-not-print" not in rendered


def test_default_platform_uses_runtime_platform(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(processes_module.sys, "platform", "win32")

    assert redact_command_line(["worker app"], "full") == '"worker app"'


def test_redaction_rejects_mode_outside_configuration_contract() -> None:
    with pytest.raises(ValueError, match="none, redacted, or full"):
        redact_command_line(["worker"], "raw")  # type: ignore[arg-type]


@pytest.mark.parametrize("mode", ["none", "redacted", "full"])
def test_collector_accepts_every_configured_mode(mode: str) -> None:
    collector = ProcessesCollector(mode)  # type: ignore[arg-type]

    assert collector.name == "processes"


def test_collector_rejects_mode_outside_configuration_contract() -> None:
    with pytest.raises(ValueError, match="none, redacted, or full"):
        ProcessesCollector("raw")  # type: ignore[arg-type]


@pytest.mark.parametrize("limit", [0, -1])
def test_collector_rejects_nonpositive_limits(limit: int) -> None:
    with pytest.raises(ValueError, match="limit"):
        ProcessesCollector("redacted", limit=limit)


def test_processes_preserve_schema_sort_limit_redact_and_report_partial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    processes = [
        FakeProcess(process_info(1, 1024, ["worker", "--token", "secret-one"])),
        FakeProcess(error=psutil.AccessDenied(pid=2, name="private-secret-process")),
        FakeProcess(process_info(3, 4096, ["server app", "--port", "443"])),
        FakeProcess(error=psutil.NoSuchProcess(pid=4, name="gone-secret-process")),
    ]
    requested_attrs: list[list[str]] = []

    def process_iter(attrs: list[str]) -> list[FakeProcess]:
        requested_attrs.append(attrs)
        return processes

    monkeypatch.setattr(processes_module.psutil, "process_iter", process_iter)

    result = ProcessesCollector("redacted", limit=1).collect()

    assert result.status is CollectorStatus.PARTIAL
    assert result.error_code == "process_access_partial"
    assert result.error_message == "some processes unavailable"
    assert result.data == {
        "processes": [
            {
                "pid": 3,
                "name": "process-3",
                "user": "employee",
                "status": "running",
                "cpu_pct": 4.5,
                "mem_rss_kb": 4,
                "exe": "/usr/bin/process-3",
                "cmdline": "'server app' --port 443",
                "started": "2023-11-14T22:13:20+00:00",
            }
        ]
    }
    assert requested_attrs == [
        [
            "pid",
            "name",
            "username",
            "status",
            "cpu_percent",
            "memory_info",
            "exe",
            "cmdline",
            "create_time",
        ]
    ]
    assert "secret" not in repr(result)
    assert "private" not in repr(result)
    json.dumps(result.data, allow_nan=False)


def test_no_such_process_is_a_successful_race_and_ties_sort_by_pid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        processes_module.psutil,
        "process_iter",
        lambda attrs: [
            FakeProcess(process_info(9, 2048, ["nine"])),
            FakeProcess(error=psutil.NoSuchProcess(pid=8)),
            FakeProcess(process_info(3, 2048, ["three"])),
        ],
    )

    result = ProcessesCollector("full").collect()

    assert result.status is CollectorStatus.SUCCESS
    assert [process["pid"] for process in result.data["processes"]] == [3, 9]


def test_missing_none_and_non_string_values_remain_safe_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class SecretValue:
        def __str__(self) -> str:
            return "do-not-print-secret"

        def __repr__(self) -> str:
            return "do-not-print-secret"

    monkeypatch.setattr(
        processes_module.psutil,
        "process_iter",
        lambda attrs: [
            FakeProcess(
                {
                    "pid": None,
                    "name": SecretValue(),
                    "username": None,
                    "status": SecretValue(),
                    "cpu_percent": None,
                    "memory_info": None,
                    "exe": SecretValue(),
                    "cmdline": ["worker", SecretValue(), "do-not-print-secret"],
                    "create_time": None,
                }
            )
        ],
    )

    result = ProcessesCollector("redacted").collect()

    assert result.status is CollectorStatus.SUCCESS
    assert result.data == {
        "processes": [
            {
                "pid": 0,
                "name": None,
                "user": None,
                "status": None,
                "cpu_pct": 0.0,
                "mem_rss_kb": 0,
                "exe": None,
                "cmdline": "",
                "started": None,
            }
        ]
    }
    assert "do-not-print-secret" not in repr(result)
    json.dumps(result.data, allow_nan=False)


def test_none_mode_never_retains_process_arguments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        processes_module.psutil,
        "process_iter",
        lambda attrs: [FakeProcess(process_info(1, 1024, ["worker", "plain-secret"]))],
    )

    result = ProcessesCollector("none").collect()

    assert result.data["processes"][0]["cmdline"] == ""
    assert "plain-secret" not in repr(result)
