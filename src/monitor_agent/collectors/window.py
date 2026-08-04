from __future__ import annotations

import subprocess
import sys
from monitor_agent.models import CollectorPayload, JSONValue


class ActiveWindowCollector:
    name = "active_window"

    def collect(self) -> CollectorPayload:
        win_info: dict[str, JSONValue] = {"title": None, "app": None, "pid": None}
        try:
            if sys.platform == "linux":
                out = subprocess.check_output(
                    ["xdotool", "getactivewindow", "getwindowname"],
                    stderr=subprocess.DEVNULL,
                    text=True,
                ).strip()
                win_info["title"] = out if out else None
            elif sys.platform == "win32":
                import ctypes

                hwnd = ctypes.windll.user32.GetForegroundWindow()
                length = ctypes.windll.user32.GetWindowTextLengthW(hwnd)
                buf = ctypes.create_unicode_buffer(length + 1)
                ctypes.windll.user32.GetWindowTextW(hwnd, buf, length + 1)
                win_info["title"] = buf.value if buf.value else None
            elif sys.platform == "darwin":
                cmd = 'tell application "System Events" to get name of first process whose frontmost is true'
                out = subprocess.check_output(
                    ["osascript", "-e", cmd],
                    stderr=subprocess.DEVNULL,
                    text=True,
                ).strip()
                win_info["app"] = out if out else None
        except Exception:
            pass
        return CollectorPayload(data={"active_window": win_info})
