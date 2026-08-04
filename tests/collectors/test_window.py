from __future__ import annotations

import subprocess

import pytest

from monitor_agent.collectors import window
from monitor_agent.collectors.window import ActiveWindowCollector
from monitor_agent.models import CollectorStatus


def test_active_window_is_disabled_without_explicit_configuration() -> None:
    payload = ActiveWindowCollector(False, "linux").collect()

    assert payload.status is CollectorStatus.DISABLED
    assert payload.data == {"active_window": {"title": None, "app": None, "pid": None}}


def test_linux_active_window_uses_bounded_xdotool_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[list[str], float]] = []

    def check_output(
        command: list[str],
        *,
        stderr: int,
        text: bool,
        timeout: float,
    ) -> str:
        assert stderr == subprocess.DEVNULL
        assert text is True
        calls.append((command, timeout))
        return "Quarterly report\n"

    monkeypatch.setattr(window.subprocess, "check_output", check_output)

    payload = ActiveWindowCollector(True, "linux").collect()

    assert payload.status is CollectorStatus.SUCCESS
    assert payload.data == {
        "active_window": {
            "title": "Quarterly report",
            "app": None,
            "pid": None,
        }
    }
    assert calls == [
        (
            ["xdotool", "getactivewindow", "getwindowname"],
            2.5,
        )
    ]


def test_active_window_reports_missing_interactive_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_command(*args: object, **kwargs: object) -> str:
        raise FileNotFoundError("xdotool")

    monkeypatch.setattr(window.subprocess, "check_output", fail_command)

    payload = ActiveWindowCollector(True, "linux").collect()

    assert payload.status is CollectorStatus.PARTIAL
    assert payload.error_code == "active_window_unavailable"
    assert payload.error_message == "interactive active-window data unavailable"
    assert payload.data == {"active_window": {"title": None, "app": None, "pid": None}}
