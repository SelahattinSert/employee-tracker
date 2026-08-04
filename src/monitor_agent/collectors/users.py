from __future__ import annotations

import math
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Protocol, cast

import psutil  # type: ignore[import-untyped]

from monitor_agent.models import CollectorPayload, CollectorStatus, JSONValue


class _UserSession(Protocol):
    name: object
    terminal: object
    host: object
    started: object
    pid: object


def _optional_string(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _optional_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _utc_timestamp(value: object) -> str | None:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    converted = float(value)
    if not math.isfinite(converted):
        return None
    try:
        return datetime.fromtimestamp(converted, tz=UTC).isoformat()
    except (OSError, OverflowError, ValueError):
        return None


def _user_record(session: _UserSession) -> dict[str, JSONValue]:
    return {
        "name": _optional_string(session.name),
        "terminal": _optional_string(session.terminal),
        "host": _optional_string(session.host),
        "started": _utc_timestamp(session.started),
        "pid": _optional_int(session.pid),
    }


class UsersCollector:
    name = "users"

    def collect(self) -> CollectorPayload:
        try:
            sessions = cast(Iterable[_UserSession], psutil.users())
            user_values: list[JSONValue] = [_user_record(session) for session in sessions]
        except OSError:
            return CollectorPayload(
                data={"users": []},
                status=CollectorStatus.PARTIAL,
                error_code="user_access_partial",
                error_message="user sessions unavailable",
            )
        data: dict[str, JSONValue] = {"users": user_values}
        return CollectorPayload(data=data)
