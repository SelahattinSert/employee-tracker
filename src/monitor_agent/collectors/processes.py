from __future__ import annotations

import math
import shlex
import subprocess
import sys
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Protocol, cast

import psutil  # type: ignore[import-untyped]

from monitor_agent.config import ProcessCmdlineMode
from monitor_agent.models import CollectorPayload, CollectorStatus, JSONValue

SECRET_FLAGS = frozenset(
    {
        "--api-key",
        "--apikey",
        "--access-token",
        "--authorization",
        "--password",
        "--secret",
        "--token",
    }
)
SECRET_ASSIGNMENT_PARTS = ("API_KEY", "AUTH", "PASSWORD", "SECRET", "TOKEN")

_PROCESS_ATTRIBUTES = [
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
_ATTRIBUTE_UNAVAILABLE = object()


class _Process(Protocol):
    @property
    def info(self) -> Mapping[str, object]: ...


def _validated_mode(mode: object, parameter: str) -> ProcessCmdlineMode:
    if mode not in {"none", "redacted", "full"}:
        raise ValueError(f"{parameter} must be none, redacted, or full")
    return mode


def _redacted_arguments(arguments: Sequence[str]) -> list[str]:
    redacted: list[str] = []
    mask_next = False
    for argument in arguments:
        name, separator, _ = argument.partition("=")
        is_secret_flag = name.casefold() in SECRET_FLAGS
        if mask_next:
            redacted.append("***")
            mask_next = False
            if not is_secret_flag:
                continue

        if is_secret_flag:
            if separator:
                redacted.append(f"{name}=***")
            else:
                redacted.append(argument)
                mask_next = True
            continue

        if separator and any(part in name.upper() for part in SECRET_ASSIGNMENT_PARTS):
            redacted.append(f"{name}=***")
            continue

        redacted.append(argument)
    return redacted


def _join_arguments(arguments: Sequence[str], platform_name: str) -> str:
    if platform_name == "win32":
        return subprocess.list2cmdline(list(arguments))
    return shlex.join(arguments).replace("'***'", "***")


def redact_command_line(
    arguments: Sequence[str],
    mode: ProcessCmdlineMode,
    *,
    platform_name: str | None = None,
) -> str:
    """Render process arguments while applying the configured privacy policy."""
    selected_mode = _validated_mode(mode, "mode")
    if selected_mode == "none":
        return ""

    safe_arguments = (
        _redacted_arguments(arguments) if selected_mode == "redacted" else list(arguments)
    )
    platform = sys.platform if platform_name is None else platform_name
    return _join_arguments(safe_arguments, platform)


def _optional_string(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _safe_int(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _safe_float(value: object) -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        converted = float(value)
        if math.isfinite(converted):
            return converted
    return 0.0


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


def _command_arguments(value: object) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return ()
    arguments: list[str] = []
    for argument in value:
        if not isinstance(argument, str):
            return ()
        arguments.append(argument)
    return tuple(arguments)


def _memory_rss_kb(value: object) -> int:
    rss = getattr(value, "rss", 0)
    return max(0, _safe_int(rss)) // 1024


def _process_record(
    info: Mapping[str, object], mode: ProcessCmdlineMode
) -> dict[str, JSONValue]:
    return {
        "pid": _safe_int(info.get("pid")),
        "name": _optional_string(info.get("name")),
        "user": _optional_string(info.get("username")),
        "status": _optional_string(info.get("status")),
        "cpu_pct": _safe_float(info.get("cpu_percent")),
        "mem_rss_kb": _memory_rss_kb(info.get("memory_info")),
        "exe": _optional_string(info.get("exe")),
        "cmdline": redact_command_line(_command_arguments(info.get("cmdline")), mode),
        "started": _utc_timestamp(info.get("create_time")),
    }


class ProcessesCollector:
    name = "processes"

    def __init__(self, cmdline_mode: ProcessCmdlineMode, limit: int = 100) -> None:
        self._cmdline_mode = _validated_mode(cmdline_mode, "cmdline_mode")
        if limit < 1:
            raise ValueError("limit must be at least 1")
        self._limit = limit

    def collect(self) -> CollectorPayload:
        processes = cast(
            Iterable[_Process],
            psutil.process_iter(_PROCESS_ATTRIBUTES, ad_value=_ATTRIBUTE_UNAVAILABLE),
        )
        records: list[dict[str, JSONValue]] = []
        access_partial = False
        for process in processes:
            try:
                info = process.info
                if any(value is _ATTRIBUTE_UNAVAILABLE for value in info.values()):
                    access_partial = True
                    continue
                records.append(_process_record(info, self._cmdline_mode))
            except psutil.AccessDenied:
                access_partial = True
            except psutil.NoSuchProcess:
                continue

        records.sort(
            key=lambda record: (
                -cast(int, record["mem_rss_kb"]),
                cast(int, record["pid"]),
            )
        )
        process_values: list[JSONValue] = [record for record in records[: self._limit]]
        data: dict[str, JSONValue] = {"processes": process_values}
        if access_partial:
            return CollectorPayload(
                data=data,
                status=CollectorStatus.PARTIAL,
                error_code="process_access_partial",
                error_message="some processes unavailable",
            )
        return CollectorPayload(data=data)
