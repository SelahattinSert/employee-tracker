from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TypeAlias

JSONValue: TypeAlias = (
    None | bool | int | float | str | list["JSONValue"] | dict[str, "JSONValue"]
)


class CollectorStatus(StrEnum):
    SUCCESS = "success"
    PARTIAL = "partial"
    DISABLED = "disabled"
    TIMED_OUT = "timed_out"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class CollectorPayload:
    data: JSONValue
    status: CollectorStatus = CollectorStatus.SUCCESS
    error_code: str | None = None
    error_message: str | None = None


@dataclass(frozen=True, slots=True)
class CollectorResult:
    name: str
    status: CollectorStatus
    duration_ms: int
    data: JSONValue
    error_code: str | None = None
    error_message: str | None = None


@dataclass(frozen=True, slots=True)
class CollectionBatch:
    results: tuple[CollectorResult, ...]
    duration_ms: int
