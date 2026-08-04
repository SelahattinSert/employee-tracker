from __future__ import annotations

import base64
import io
import os
from datetime import UTC, datetime
from monitor_agent.models import CollectorPayload, JSONValue


class ScreenshotCollector:
    name = "screenshot"

    def collect(self) -> CollectorPayload:
        if os.environ.get("MONITOR_SCREENSHOT_ENABLED", "0") != "1":
            return CollectorPayload(data={"screenshot": None})
        try:
            from PIL import ImageGrab

            img = ImageGrab.grab()
            buf = io.BytesIO()
            img.save(buf, format="PNG", optimize=True)
            shot_data: dict[str, JSONValue] = {
                "captured_at": datetime.now(tz=UTC).isoformat(),
                "format": "image/png",
                "size_bytes": buf.tell(),
                "data_b64": base64.b64encode(buf.getvalue()).decode("utf-8"),
            }
            return CollectorPayload(data={"screenshot": shot_data})
        except Exception:
            return CollectorPayload(data={"screenshot": None})
