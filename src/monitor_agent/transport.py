from __future__ import annotations

import math
import random
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import cast

import requests

from monitor_agent import __version__
from monitor_agent.config import AgentConfig
from monitor_agent.models import DeliveryKind, DeliveryResult, JSONValue

_REQUIRED_HEADER_FIELDS = ("event_id", "event", "machine_id", "schema_version")
_SUCCESS_MESSAGE = "delivered"
_AUTHENTICATION_MESSAGE = "authentication failed"
_PERMANENT_MESSAGE = "delivery rejected"
_STATUS_FAILURE_MESSAGE = "delivery failed"
_TIMEOUT_MESSAGE = "request timed out"
_CONNECTION_MESSAGE = "connection failed"
_REQUEST_MESSAGE = "request failed"


def _current_time() -> datetime:
    return datetime.now(UTC)


class TelemetryTransport:
    def __init__(
        self,
        config: AgentConfig,
        *,
        session: requests.Session | None = None,
        max_attempts: int = 4,
        sleep: Callable[[float], None] = time.sleep,
        random_value: Callable[[], float] = random.random,
    ) -> None:
        if not config.collector_uri or not config.api_token:
            raise ValueError("transport configuration is incomplete")
        if not isinstance(max_attempts, int) or isinstance(max_attempts, bool) or max_attempts <= 0:
            raise ValueError("max_attempts must be positive")

        self._config = config
        self._collector_uri = config.collector_uri
        self._api_token = config.api_token
        self._session = requests.Session() if session is None else session
        self._max_attempts = max_attempts
        self._sleep = sleep
        self._random_value = random_value

    def close(self) -> None:
        self._session.close()

    def send(self, payload: Mapping[str, JSONValue]) -> DeliveryResult:
        self._validate_payload(payload)
        headers = self._headers(payload)
        for attempt in range(1, self._max_attempts + 1):
            try:
                response = self._session.post(
                    self._collector_uri,
                    json=payload,
                    headers=headers,
                    timeout=(self._config.connect_timeout_sec, self._config.read_timeout_sec),
                    verify=True if self._config.ca_bundle is None else str(self._config.ca_bundle),
                )
            except requests.Timeout:
                result = DeliveryResult(DeliveryKind.RETRIABLE, None, attempt, _TIMEOUT_MESSAGE)
                retry_after = None
            except requests.ConnectionError:
                result = DeliveryResult(DeliveryKind.RETRIABLE, None, attempt, _CONNECTION_MESSAGE)
                retry_after = None
            except requests.RequestException:
                result = DeliveryResult(DeliveryKind.RETRIABLE, None, attempt, _REQUEST_MESSAGE)
                retry_after = None
            else:
                status_code = self._status_code(response)
                kind = self._classify_status(status_code)
                result = DeliveryResult(kind, status_code, attempt, self._message_for(kind))
                retry_after = (
                    self._retry_after(response) if kind is DeliveryKind.RETRIABLE else None
                )

            if result.kind is not DeliveryKind.RETRIABLE or attempt == self._max_attempts:
                return result
            self._sleep(self._retry_delay(attempt, retry_after))

        raise AssertionError("retry loop must return a delivery result")

    @staticmethod
    def _validate_payload(payload: Mapping[str, JSONValue]) -> None:
        if any(
            not isinstance(payload.get(field), str) or not payload[field]
            for field in _REQUIRED_HEADER_FIELDS
        ):
            raise ValueError("payload is missing required delivery fields")

    def _headers(self, payload: Mapping[str, JSONValue]) -> dict[str, str]:
        return {
            "Accept": "application/json",
            "Authorization": f"Bearer {self._api_token}",
            "Content-Type": "application/json",
            "Idempotency-Key": cast(str, payload["event_id"]),
            "User-Agent": f"monitor-agent/{__version__}",
            "X-Event-Type": cast(str, payload["event"]),
            "X-Machine-ID": cast(str, payload["machine_id"]),
            "X-Schema-Ver": cast(str, payload["schema_version"]),
        }

    @staticmethod
    def _status_code(response: object) -> int | None:
        status_code = getattr(response, "status_code", None)
        if isinstance(status_code, int) and not isinstance(status_code, bool):
            return status_code
        return None

    @staticmethod
    def _classify_status(status_code: int | None) -> DeliveryKind:
        if status_code is not None and 200 <= status_code <= 299:
            return DeliveryKind.SUCCESS
        if status_code in {401, 403}:
            return DeliveryKind.AUTHENTICATION
        if status_code in {408, 425, 429} or (
            status_code is not None and 500 <= status_code <= 599
        ):
            return DeliveryKind.RETRIABLE
        return DeliveryKind.PERMANENT

    @staticmethod
    def _message_for(kind: DeliveryKind) -> str:
        if kind is DeliveryKind.SUCCESS:
            return _SUCCESS_MESSAGE
        if kind is DeliveryKind.AUTHENTICATION:
            return _AUTHENTICATION_MESSAGE
        if kind is DeliveryKind.PERMANENT:
            return _PERMANENT_MESSAGE
        return _STATUS_FAILURE_MESSAGE

    def _retry_delay(self, retry_number: int, retry_after: float | None) -> float:
        maximum = min(30.0, 0.5 * float(2 ** (retry_number - 1)))
        jittered_delay = float(maximum * self._safe_random_value())
        if retry_after is None:
            return jittered_delay
        return float(max(retry_after, jittered_delay))

    def _safe_random_value(self) -> float:
        try:
            value = float(self._random_value())
        except (TypeError, ValueError):
            return 0.0
        if not math.isfinite(value):
            return 0.0
        return min(1.0, max(0.0, value))

    @staticmethod
    def _retry_after(response: object) -> float | None:
        headers = getattr(response, "headers", None)
        if not isinstance(headers, Mapping):
            return None
        raw_value = headers.get("Retry-After")
        if not isinstance(raw_value, str):
            return None
        return TelemetryTransport._parse_retry_after(raw_value)

    @staticmethod
    def _parse_retry_after(value: str) -> float | None:
        try:
            delay = float(value)
        except ValueError:
            delay = None
        if delay is not None:
            if not math.isfinite(delay):
                return None
            return min(60.0, max(0.0, delay))

        try:
            retry_at = parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return None
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=UTC)
        delay = (retry_at.astimezone(UTC) - _current_time()).total_seconds()
        return min(60.0, max(0.0, delay))
