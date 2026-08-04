from __future__ import annotations

from typing import Protocol

from monitor_agent.models import CollectorPayload


class Collector(Protocol):
    name: str

    def collect(self) -> CollectorPayload: ...
