from __future__ import annotations

import base64
import importlib
import io
from datetime import UTC, datetime
from typing import Protocol, cast

from monitor_agent.models import CollectorPayload, CollectorStatus, JSONValue


class _CapturedImage(Protocol):
    def save(self, destination: io.BytesIO, *, format: str, optimize: bool) -> None: ...


class _ImageGrabModule(Protocol):
    def grab(self) -> _CapturedImage: ...


def _utc_now() -> datetime:
    return datetime.now(tz=UTC)


def _capture_png() -> bytes:
    image_grab = cast(_ImageGrabModule, importlib.import_module("PIL.ImageGrab"))
    image = image_grab.grab()
    destination = io.BytesIO()
    image.save(destination, format="PNG", optimize=True)
    return destination.getvalue()


class ScreenshotCollector:
    name = "screenshot"

    def __init__(self, enabled: bool, max_bytes: int) -> None:
        if max_bytes < 1:
            raise ValueError("max_bytes must be at least 1")
        self._enabled = enabled
        self._max_bytes = max_bytes

    def collect(self) -> CollectorPayload:
        if not self._enabled:
            return CollectorPayload(
                data={"screenshot": None},
                status=CollectorStatus.DISABLED,
            )
        try:
            png_bytes = _capture_png()
        except (ImportError, OSError, RuntimeError):
            return CollectorPayload(
                data={"screenshot": None},
                status=CollectorStatus.PARTIAL,
                error_code="screenshot_unavailable",
                error_message="interactive screenshot capture unavailable",
            )
        if len(png_bytes) > self._max_bytes:
            return CollectorPayload(
                data={"screenshot": None},
                status=CollectorStatus.PARTIAL,
                error_code="screenshot_too_large",
                error_message="captured screenshot exceeded configured size limit",
            )

        shot_data: dict[str, JSONValue] = {
            "captured_at": _utc_now().isoformat(),
            "format": "image/png",
            "size_bytes": len(png_bytes),
            "data_b64": base64.b64encode(png_bytes).decode("ascii"),
        }
        return CollectorPayload(data={"screenshot": shot_data})
