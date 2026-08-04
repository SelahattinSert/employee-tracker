from __future__ import annotations

import base64
from datetime import UTC, datetime

import pytest

from monitor_agent.collectors import screenshot
from monitor_agent.collectors.screenshot import ScreenshotCollector
from monitor_agent.models import CollectorStatus


def test_screenshot_is_disabled_by_default() -> None:
    payload = ScreenshotCollector(False, 5242880).collect()

    assert payload.status is CollectorStatus.DISABLED
    assert payload.data == {"screenshot": None}


def test_screenshot_encodes_a_bounded_png(monkeypatch: pytest.MonkeyPatch) -> None:
    captured_at = datetime(2026, 8, 4, 9, 30, tzinfo=UTC)
    monkeypatch.setattr(screenshot, "_capture_png", lambda: b"PNG")
    monkeypatch.setattr(screenshot, "_utc_now", lambda: captured_at)

    payload = ScreenshotCollector(True, 1024).collect()

    assert payload.status is CollectorStatus.SUCCESS
    assert payload.data == {
        "screenshot": {
            "captured_at": captured_at.isoformat(),
            "format": "image/png",
            "size_bytes": 3,
            "data_b64": base64.b64encode(b"PNG").decode("ascii"),
        }
    }


def test_screenshot_rejects_png_over_configured_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(screenshot, "_capture_png", lambda: b"12345")

    payload = ScreenshotCollector(True, 4).collect()

    assert payload.status is CollectorStatus.PARTIAL
    assert payload.error_code == "screenshot_too_large"
    assert payload.error_message == "captured screenshot exceeded configured size limit"
    assert payload.data == {"screenshot": None}


def test_screenshot_reports_capture_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_capture() -> bytes:
        raise OSError("desktop unavailable")

    monkeypatch.setattr(screenshot, "_capture_png", fail_capture)

    payload = ScreenshotCollector(True, 1024).collect()

    assert payload.status is CollectorStatus.PARTIAL
    assert payload.error_code == "screenshot_unavailable"
    assert payload.error_message == "interactive screenshot capture unavailable"
    assert payload.data == {"screenshot": None}
