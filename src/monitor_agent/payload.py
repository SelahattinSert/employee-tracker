from __future__ import annotations

import copy
import platform
import re
import sys
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import cast
from uuid import UUID, uuid4

from monitor_agent import __version__
from monitor_agent.identity import MachineIdentity
from monitor_agent.models import CollectionBatch, CollectorStatus, JSONValue

_EVENT_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_SECTION_NAMES = (
    "system",
    "users",
    "cpu",
    "memory",
    "disks",
    "network",
    "processes",
    "software",
)


def _default_sections() -> dict[str, JSONValue]:
    return {
        "system": {},
        "users": [],
        "cpu": {},
        "memory": {},
        "disks": [],
        "network": {"adapters": [], "connections": [], "io": {}},
        "processes": [],
        "software": [],
    }


def _timestamp(now: datetime | None) -> str:
    timestamp = datetime.now(UTC) if now is None else now
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    return timestamp.astimezone(UTC).isoformat()


def build_payload(
    event: str,
    identity: MachineIdentity,
    batch: CollectionBatch,
    *,
    now: datetime | None = None,
    event_id: UUID | None = None,
) -> dict[str, JSONValue]:
    if _EVENT_PATTERN.fullmatch(event) is None:
        raise ValueError("invalid event name")

    sections = _default_sections()
    collectors: dict[str, JSONValue] = {}
    for result in batch.results:
        collectors[result.name] = {
            "status": result.status.value,
            "duration_ms": result.duration_ms,
            "error_code": result.error_code,
            "error_message": result.error_message,
        }
        if result.status not in (CollectorStatus.SUCCESS, CollectorStatus.PARTIAL):
            continue
        if not isinstance(result.data, Mapping):
            continue
        data = cast(Mapping[str, JSONValue], result.data)
        for section_name in _SECTION_NAMES:
            if section_name in data:
                sections[section_name] = copy.deepcopy(data[section_name])

    payload: dict[str, JSONValue] = {
        "schema_version": "1.0",
        "event": event,
        "timestamp": _timestamp(now),
        "machine_id": identity.value,
        **sections,
        "event_id": str(uuid4() if event_id is None else event_id),
        "agent": {
            "version": __version__,
            "python": platform.python_version(),
            "platform": sys.platform,
            "collection_duration_ms": batch.duration_ms,
            "identity_source": identity.source,
            "collectors": collectors,
        },
    }
    return payload
