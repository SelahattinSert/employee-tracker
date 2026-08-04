from __future__ import annotations

import ctypes
import subprocess
import sys
from typing import Protocol, cast

import psutil  # type: ignore[import-untyped]

from monitor_agent.models import CollectorPayload, CollectorStatus, JSONValue

_COMMAND_TIMEOUT_SEC = 2.5


class _User32(Protocol):
    def GetForegroundWindow(self) -> int: ...

    def GetWindowTextLengthW(self, window: int) -> int: ...

    def GetWindowTextW(self, window: int, buffer: object, length: int) -> int: ...

    def GetWindowThreadProcessId(self, window: int, process_id: object) -> int: ...


class _WindowsLibraries(Protocol):
    user32: _User32


def _empty_window() -> dict[str, JSONValue]:
    return {"title": None, "app": None, "pid": None}


def _collect_linux() -> dict[str, JSONValue]:
    title = subprocess.check_output(
        ["xdotool", "getactivewindow", "getwindowname"],
        stderr=subprocess.DEVNULL,
        text=True,
        timeout=_COMMAND_TIMEOUT_SEC,
    ).strip()
    return {"title": title or None, "app": None, "pid": None}


def _collect_windows() -> dict[str, JSONValue]:
    libraries = cast(_WindowsLibraries | None, getattr(ctypes, "windll", None))
    if libraries is None:
        raise OSError("Windows desktop APIs unavailable")
    user32 = libraries.user32
    window = user32.GetForegroundWindow()
    if not window:
        raise OSError("foreground window unavailable")
    length = user32.GetWindowTextLengthW(window)
    buffer = ctypes.create_unicode_buffer(max(1, length + 1))
    user32.GetWindowTextW(window, buffer, len(buffer))
    process_id = ctypes.c_ulong()
    user32.GetWindowThreadProcessId(window, ctypes.byref(process_id))
    pid = int(process_id.value) or None
    app: str | None = None
    if pid is not None:
        try:
            app = psutil.Process(pid).name()
        except (psutil.AccessDenied, psutil.NoSuchProcess, OSError):
            app = None
    return {
        "title": buffer.value or None,
        "app": app,
        "pid": pid,
    }


def _collect_macos() -> dict[str, JSONValue]:
    script = (
        'tell application "System Events" to tell first process whose frontmost is true '
        "to return {name, unix id}"
    )
    output = subprocess.check_output(
        ["osascript", "-e", script],
        stderr=subprocess.DEVNULL,
        text=True,
        timeout=_COMMAND_TIMEOUT_SEC,
    ).strip()
    app, separator, raw_pid = output.rpartition(", ")
    if not separator:
        app = output
        raw_pid = ""
    try:
        pid = int(raw_pid)
    except ValueError:
        pid = None
    return {"title": None, "app": app or None, "pid": pid}


class ActiveWindowCollector:
    name = "active_window"

    def __init__(self, enabled: bool, platform_name: str | None = None) -> None:
        self._enabled = enabled
        self._platform_name = sys.platform if platform_name is None else platform_name

    def collect(self) -> CollectorPayload:
        if not self._enabled or self._platform_name not in {"linux", "win32", "darwin"}:
            return CollectorPayload(
                data={"active_window": _empty_window()},
                status=CollectorStatus.DISABLED,
            )

        try:
            if self._platform_name == "linux":
                window = _collect_linux()
            elif self._platform_name == "win32":
                window = _collect_windows()
            else:
                window = _collect_macos()
        except (AttributeError, OSError, RuntimeError, subprocess.SubprocessError):
            return CollectorPayload(
                data={"active_window": _empty_window()},
                status=CollectorStatus.PARTIAL,
                error_code="active_window_unavailable",
                error_message="interactive active-window data unavailable",
            )
        return CollectorPayload(data={"active_window": window})
