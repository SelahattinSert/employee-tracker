from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
import requests

from monitor_agent.config import load_config
from monitor_agent.models import DeliveryKind
from monitor_agent.transport import TelemetryTransport


class FakeSession:
    def __init__(self, outcomes: Iterator[object]) -> None:
        self.outcomes = outcomes
        self.calls: list[tuple[tuple[object, ...], dict[str, object]]] = []
        self.close_calls = 0

    def post(self, *args: object, **kwargs: object) -> object:
        self.calls.append((args, kwargs))
        outcome = next(self.outcomes)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    def close(self) -> None:
        self.close_calls += 1


def response(status: int, headers: dict[str, str] | None = None) -> object:
    return SimpleNamespace(status_code=status, headers=headers or {})


def config() -> object:
    return load_config(
        {
            "MONITOR_COLLECTOR_URI": "https://collector.internal/api/v1/telemetry",
            "MONITOR_API_TOKEN": "token-value",
        },
        platform_name="linux",
    )


def telemetry() -> dict[str, str]:
    return {
        "event_id": "12345678-1234-4678-9234-567812345678",
        "event": "heartbeat",
        "machine_id": "machine-123",
        "schema_version": "1.0",
    }


def test_close_delegates_to_the_injected_session_once_per_call() -> None:
    session = FakeSession(iter([]))
    transport = TelemetryTransport(config(), session=session)  # type: ignore[arg-type]

    transport.close()
    transport.close()

    assert session.close_calls == 2


@pytest.mark.parametrize("status", [200, 299])
def test_success_statuses_use_json_timeout_tls_and_safe_headers(status: int) -> None:
    session = FakeSession(iter([response(status)]))

    result = TelemetryTransport(config(), session=session).send(telemetry())  # type: ignore[arg-type]

    assert result.kind is DeliveryKind.SUCCESS
    assert result.status_code == status
    assert result.attempts == 1
    assert result.message == "delivered"
    _, kwargs = session.calls[0]
    assert kwargs["json"] == telemetry()
    assert kwargs["timeout"] == (5.0, 15.0)
    assert kwargs["verify"] is True
    assert kwargs["headers"] == {
        "Accept": "application/json",
        "Authorization": "Bearer token-value",
        "Content-Type": "application/json",
        "Idempotency-Key": telemetry()["event_id"],
        "User-Agent": "monitor-agent/2.0.0",
        "X-Event-Type": "heartbeat",
        "X-Machine-ID": "machine-123",
        "X-Schema-Ver": "1.0",
    }


def test_configured_ca_bundle_is_used_for_tls_verification(tmp_path: Any) -> None:
    certificate = tmp_path / "collector.pem"
    certificate.write_text("certificate")
    configured = load_config(
        {
            "MONITOR_COLLECTOR_URI": "https://collector.internal/api/v1/telemetry",
            "MONITOR_API_TOKEN": "token-value",
            "MONITOR_CA_BUNDLE": str(certificate),
        },
        platform_name="linux",
    )
    session = FakeSession(iter([response(200)]))

    TelemetryTransport(configured, session=session).send(telemetry())  # type: ignore[arg-type]

    assert session.calls[0][1]["verify"] == str(certificate)


@pytest.mark.parametrize("status", [408, 425, 429, 500, 599])
def test_retriable_statuses_back_off_then_succeed(status: int) -> None:
    delays: list[float] = []
    session = FakeSession(iter([response(status), response(200)]))

    result = TelemetryTransport(
        config(), session=session, sleep=delays.append, random_value=lambda: 0.5
    ).send(telemetry())  # type: ignore[arg-type]

    assert result.kind is DeliveryKind.SUCCESS
    assert result.attempts == 2
    assert delays == [0.25]


@pytest.mark.parametrize("status", [401, 403])
def test_authentication_statuses_are_not_retried(status: int) -> None:
    session = FakeSession(iter([response(status), response(200)]))

    result = TelemetryTransport(config(), session=session).send(telemetry())  # type: ignore[arg-type]

    assert result.kind is DeliveryKind.AUTHENTICATION
    assert result.status_code == status
    assert result.attempts == 1
    assert result.message == "authentication failed"
    assert len(session.calls) == 1


@pytest.mark.parametrize("status", [300, 399, 400, 422, 499, 600])
def test_unexpected_and_permanent_statuses_are_not_retried(status: int) -> None:
    session = FakeSession(iter([response(status), response(200)]))

    result = TelemetryTransport(config(), session=session).send(telemetry())  # type: ignore[arg-type]

    assert result.kind is DeliveryKind.PERMANENT
    assert result.status_code == status
    assert result.attempts == 1
    assert result.message == "delivery rejected"
    assert len(session.calls) == 1


def test_malformed_response_status_is_a_sanitized_permanent_failure() -> None:
    session = FakeSession(iter([SimpleNamespace(status_code="not-a-status", headers={})]))

    result = TelemetryTransport(config(), session=session).send(telemetry())  # type: ignore[arg-type]

    assert result.kind is DeliveryKind.PERMANENT
    assert result.status_code is None
    assert result.message == "delivery rejected"


def test_response_without_headers_ignores_retry_after() -> None:
    delays: list[float] = []
    session = FakeSession(iter([SimpleNamespace(status_code=503, headers=None), response(200)]))

    TelemetryTransport(
        config(), session=session, sleep=delays.append, random_value=lambda: 0.5
    ).send(telemetry())  # type: ignore[arg-type]

    assert delays == [0.25]


@pytest.mark.parametrize(
    ("failure", "message"),
    [
        (requests.Timeout("secret timeout"), "request timed out"),
        (requests.ConnectionError("secret connection"), "connection failed"),
        (requests.RequestException("secret request"), "request failed"),
    ],
)
def test_request_exceptions_retry_with_sanitized_messages(
    failure: requests.RequestException, message: str
) -> None:
    session = FakeSession(iter([failure, response(200)]))

    result = TelemetryTransport(
        config(), session=session, sleep=lambda _: None
    ).send(telemetry())  # type: ignore[arg-type]

    assert result.kind is DeliveryKind.SUCCESS
    assert result.attempts == 2
    assert result.message == "delivered"


@pytest.mark.parametrize(
    ("failure", "message"),
    [
        (requests.Timeout("secret timeout"), "request timed out"),
        (requests.ConnectionError("secret connection"), "connection failed"),
        (requests.RequestException("secret request"), "request failed"),
    ],
)
def test_request_exception_exhaustion_is_retriable_and_sanitized(
    failure: requests.RequestException, message: str
) -> None:
    session = FakeSession(iter([failure, failure]))

    result = TelemetryTransport(
        config(), session=session, max_attempts=2, sleep=lambda _: None
    ).send(telemetry())  # type: ignore[arg-type]

    assert result.kind is DeliveryKind.RETRIABLE
    assert result.status_code is None
    assert result.attempts == 2
    assert result.message == message
    assert "secret" not in result.message


def test_retriable_status_exhaustion_does_not_sleep_after_final_attempt() -> None:
    delays: list[float] = []
    session = FakeSession(iter([response(503), response(503)]))

    result = TelemetryTransport(
        config(), session=session, max_attempts=2, sleep=delays.append, random_value=lambda: 1.0
    ).send(telemetry())  # type: ignore[arg-type]

    assert result.kind is DeliveryKind.RETRIABLE
    assert result.status_code == 503
    assert result.attempts == 2
    assert delays == [0.5]


@pytest.mark.parametrize(
    ("header", "expected_delay"),
    [
        ("1.5", 1.5),
        ("120", 60.0),
        ("-4", 0.25),
        ("inf", 0.25),
        ("not-a-delay", 0.25),
    ],
)
def test_retry_after_numeric_values_are_bounded_and_preferred(
    header: str, expected_delay: float
) -> None:
    delays: list[float] = []
    session = FakeSession(iter([response(429, {"Retry-After": header}), response(200)]))

    TelemetryTransport(
        config(), session=session, sleep=delays.append, random_value=lambda: 0.5
    ).send(telemetry())  # type: ignore[arg-type]

    assert delays == [expected_delay]


def test_retry_after_http_date_uses_a_fixed_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    fixed_now = datetime(2026, 7, 27, 12, 0, tzinfo=UTC)
    retry_at = fixed_now + timedelta(seconds=3)
    delays: list[float] = []
    session = FakeSession(
        iter([response(503, {"Retry-After": "Mon, 27 Jul 2026 12:00:03 GMT"}), response(200)])
    )
    monkeypatch.setattr("monitor_agent.transport._current_time", lambda: fixed_now)

    TelemetryTransport(
        config(), session=session, sleep=delays.append, random_value=lambda: 0.5
    ).send(telemetry())  # type: ignore[arg-type]

    assert retry_at > fixed_now
    assert delays == [3.0]


def test_past_http_date_uses_jittered_delay() -> None:
    delays: list[float] = []
    session = FakeSession(
        iter([response(503, {"Retry-After": "Mon, 27 Jul 2000 12:00:00 GMT"}), response(200)])
    )

    TelemetryTransport(
        config(), session=session, sleep=delays.append, random_value=lambda: 0.5
    ).send(telemetry())  # type: ignore[arg-type]

    assert delays == [0.25]


@pytest.mark.parametrize("random_value", [lambda: -1.0, lambda: float("nan"), lambda: 2.0])
def test_invalid_random_values_cannot_produce_invalid_sleep(random_value: object) -> None:
    delays: list[float] = []
    session = FakeSession(iter([response(503), response(200)]))

    TelemetryTransport(
        config(), session=session, sleep=delays.append, random_value=random_value
    ).send(telemetry())  # type: ignore[arg-type]

    assert delays == [0.0 if random_value() != 2.0 else 0.5]  # type: ignore[operator]


@pytest.mark.parametrize(
    "payload", [{}, {"event_id": "event"}, {"event_id": "event", "event": "x"}]
)
def test_payload_header_fields_are_required(payload: dict[str, str]) -> None:
    session = FakeSession(iter([response(200)]))

    with pytest.raises(ValueError, match="payload is missing required delivery fields"):
        TelemetryTransport(config(), session=session).send(payload)  # type: ignore[arg-type]

    assert session.calls == []


def test_constructor_rejects_missing_transport_configuration_and_invalid_attempts() -> None:
    no_transport = load_config({}, require_transport=False, platform_name="linux")

    with pytest.raises(ValueError, match="transport configuration is incomplete"):
        TelemetryTransport(no_transport)
    with pytest.raises(ValueError, match="transport configuration is incomplete"):
        TelemetryTransport(replace(config(), collector_uri=""))
    with pytest.raises(ValueError, match="transport configuration is incomplete"):
        TelemetryTransport(replace(config(), api_token=""))
    with pytest.raises(ValueError, match="max_attempts must be positive"):
        TelemetryTransport(config(), max_attempts=0)
    with pytest.raises(ValueError, match="max_attempts must be positive"):
        TelemetryTransport(config(), max_attempts=1.5)  # type: ignore[arg-type]


def test_results_and_validation_errors_never_expose_token() -> None:
    no_transport = load_config({}, require_transport=False, platform_name="linux")

    with pytest.raises(ValueError) as error:
        TelemetryTransport(no_transport)

    assert "token-value" not in repr(TelemetryTransport(config()))
    assert "token-value" not in str(error.value)
