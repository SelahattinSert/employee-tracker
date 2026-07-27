from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from dataclasses import replace
from pathlib import Path
from threading import Event
from typing import cast

import pytest

from monitor_agent.collectors.base import Collector
from monitor_agent.config import AgentConfig, load_config
from monitor_agent.identity import MachineIdentity
from monitor_agent.models import (
    CollectorPayload,
    CycleResult,
    DeliveryKind,
    DeliveryResult,
    JSONValue,
)
from monitor_agent.runtime import AgentRuntime
from monitor_agent.spool import Spool

_EVENT_ID_PREFIX = "12345678-1234-4678-9234-56781234"


class EmptyCollector:
    name = "empty"

    def collect(self) -> CollectorPayload:
        return CollectorPayload(data={})


class FailingCollector:
    name = "failing"

    def collect(self) -> CollectorPayload:
        raise RuntimeError("secret process --token exposed")


class FakeTransport:
    def __init__(self, results: list[DeliveryResult]) -> None:
        self.results: Iterator[DeliveryResult] = iter(results)
        self.payloads: list[Mapping[str, JSONValue]] = []
        self.close_calls = 0

    def send(self, payload: Mapping[str, JSONValue]) -> DeliveryResult:
        self.payloads.append(payload)
        return next(self.results)

    def close(self) -> None:
        self.close_calls += 1


class RaisingCloseTransport(FakeTransport):
    def close(self) -> None:
        super().close()
        raise RuntimeError("close failed")


class ScriptedEvent:
    def __init__(self, wait_results: list[bool]) -> None:
        self.wait_results: Iterator[bool] = iter(wait_results)
        self.waits: list[float] = []
        self.set_calls = 0
        self.stopped = False

    def wait(self, timeout: float) -> bool:
        self.waits.append(timeout)
        result = next(self.wait_results)
        if result:
            self.stopped = True
        return result

    def set(self) -> None:
        self.set_calls += 1
        self.stopped = True

    def is_set(self) -> bool:
        return self.stopped


def config(tmp_path: Path, *, replay_batch_size: int = 20) -> AgentConfig:
    return load_config(
        {
            "MONITOR_COLLECTOR_URI": "https://collector.internal/api/v1/telemetry",
            "MONITOR_API_TOKEN": "token",
            "MONITOR_SPOOL_PATH": str(tmp_path),
            "MONITOR_REPLAY_BATCH_SIZE": str(replay_batch_size),
            "MONITOR_STARTUP_DELAY_SEC": "0",
            "MONITOR_HEARTBEAT_SEC": "30",
        },
        platform_name="linux",
    )


def runtime(
    tmp_path: Path,
    transport: FakeTransport,
    *,
    replay_batch_size: int = 20,
    collectors: list[Collector] | None = None,
    stop_event: Event | ScriptedEvent | None = None,
) -> AgentRuntime:
    agent_config = config(tmp_path, replay_batch_size=replay_batch_size)
    return AgentRuntime(
        config=agent_config,
        identity=MachineIdentity("machine-id", "test"),
        collectors=[EmptyCollector()] if collectors is None else collectors,
        transport=transport,
        spool=Spool(
            tmp_path,
            agent_config.spool_max_bytes,
            agent_config.spool_max_age_sec,
        ),
        stop_event=Event() if stop_event is None else stop_event,
    )


def queued_payload(number: int) -> dict[str, JSONValue]:
    return {
        "schema_version": "1.0",
        "event_id": f"{_EVENT_ID_PREFIX}{number:04d}",
        "event": "heartbeat",
        "machine_id": "machine-id",
    }


def delivery(kind: DeliveryKind, status_code: int | None = None) -> DeliveryResult:
    return DeliveryResult(kind, status_code, 1, "message that must not be logged")


def test_cycle_result_is_frozen_and_slotted() -> None:
    result = CycleResult("event-id", True, False, DeliveryKind.SUCCESS)

    assert result == CycleResult("event-id", True, False, DeliveryKind.SUCCESS)
    assert not hasattr(result, "__dict__")
    with pytest.raises(AttributeError):
        result.event_id = "changed"  # type: ignore[misc]


def test_replay_sends_oldest_first_and_acks_successes(tmp_path: Path) -> None:
    transport = FakeTransport(
        [delivery(DeliveryKind.SUCCESS, 200), delivery(DeliveryKind.SUCCESS, 204)]
    )
    agent = runtime(tmp_path, transport)
    first = agent.spool.enqueue(queued_payload(1))
    second = agent.spool.enqueue(queued_payload(2))

    assert agent.replay() is True

    assert [payload["event_id"] for payload in transport.payloads] == [
        queued_payload(1)["event_id"],
        queued_payload(2)["event_id"],
    ]
    assert not first.exists()
    assert not second.exists()


def test_replay_stops_at_batch_limit_and_reports_remaining_backlog(tmp_path: Path) -> None:
    transport = FakeTransport([delivery(DeliveryKind.SUCCESS, 200)])
    agent = runtime(tmp_path, transport, replay_batch_size=1)
    agent.spool.enqueue(queued_payload(1))
    second = agent.spool.enqueue(queued_payload(2))

    assert agent.replay() is False

    assert [payload["event_id"] for payload in transport.payloads] == [
        queued_payload(1)["event_id"]
    ]
    assert second.exists()


def test_replay_rejects_permanent_and_continues_after_corrupt_record(
    tmp_path: Path,
) -> None:
    transport = FakeTransport(
        [delivery(DeliveryKind.PERMANENT, 422), delivery(DeliveryKind.SUCCESS, 200)]
    )
    agent = runtime(tmp_path, transport)
    first = agent.spool.enqueue(queued_payload(1))
    corrupt = tmp_path / "20990720T120000000000Z_corrupt.json"
    corrupt.write_text("{", encoding="utf-8")
    last = agent.spool.enqueue(queued_payload(2))

    assert agent.replay() is True

    assert not first.exists()
    assert not corrupt.exists()
    assert not last.exists()
    assert agent.spool.stats().dead_letter_count == 2
    assert [payload["event_id"] for payload in transport.payloads] == [
        queued_payload(1)["event_id"],
        queued_payload(2)["event_id"],
    ]


@pytest.mark.parametrize("kind", [DeliveryKind.AUTHENTICATION, DeliveryKind.RETRIABLE])
def test_replay_retains_record_and_stops_on_temporary_delivery_failure(
    tmp_path: Path,
    kind: DeliveryKind,
) -> None:
    transport = FakeTransport([delivery(kind, 401 if kind is DeliveryKind.AUTHENTICATION else 503)])
    agent = runtime(tmp_path, transport)
    blocked = agent.spool.enqueue(queued_payload(1))
    later = agent.spool.enqueue(queued_payload(2))

    assert agent.replay() is False

    assert blocked.exists()
    assert later.exists()
    assert len(transport.payloads) == 1


def test_replay_enforces_retention_after_processing(tmp_path: Path) -> None:
    transport = FakeTransport([])
    agent = runtime(tmp_path, transport)
    calls: list[str] = []
    pending = agent.spool.pending
    enforce_retention = agent.spool.enforce_retention

    def tracked_pending() -> list[Path]:
        calls.append("pending")
        return pending()

    def tracked_retention(**kwargs: object) -> object:
        calls.append("retention")
        return enforce_retention(**kwargs)

    agent.spool.pending = tracked_pending  # type: ignore[method-assign]
    agent.spool.enforce_retention = tracked_retention  # type: ignore[method-assign]

    assert agent.replay() is True
    assert calls == ["pending", "retention", "pending"]


def test_run_cycle_replays_backlog_before_sending_one_live_payload(tmp_path: Path) -> None:
    transport = FakeTransport(
        [delivery(DeliveryKind.SUCCESS, 200), delivery(DeliveryKind.SUCCESS, 200)]
    )
    agent = runtime(tmp_path, transport)
    agent.spool.enqueue(queued_payload(1))

    result = agent.run_cycle("heartbeat")

    assert transport.payloads[0]["event_id"] == queued_payload(1)["event_id"]
    assert result == CycleResult(
        event_id=cast(str, transport.payloads[1]["event_id"]),
        delivered=True,
        spooled=False,
        delivery_kind=DeliveryKind.SUCCESS,
    )
    assert transport.payloads[1]["event"] == "heartbeat"


def test_run_cycle_spools_live_payload_without_sending_when_backlog_remains(
    tmp_path: Path,
) -> None:
    transport = FakeTransport([delivery(DeliveryKind.SUCCESS, 200)])
    agent = runtime(tmp_path, transport, replay_batch_size=1)
    agent.spool.enqueue(queued_payload(1))
    agent.spool.enqueue(queued_payload(2))

    result = agent.run_cycle("heartbeat")

    assert result.delivered is False
    assert result.spooled is True
    assert result.delivery_kind is None
    assert len(transport.payloads) == 1
    pending_payloads = [agent.spool.load(path) for path in agent.spool.pending()]
    pending_event_ids = [
        payload["event_id"] for payload in pending_payloads if payload is not None
    ]
    assert pending_event_ids[0] == queued_payload(2)["event_id"]
    assert result.event_id == pending_event_ids[1]


@pytest.mark.parametrize("kind", [DeliveryKind.AUTHENTICATION, DeliveryKind.RETRIABLE])
def test_run_cycle_spools_live_temporary_failure_with_same_event_id(
    tmp_path: Path,
    kind: DeliveryKind,
) -> None:
    transport = FakeTransport([delivery(kind, 401 if kind is DeliveryKind.AUTHENTICATION else 503)])
    agent = runtime(tmp_path, transport)

    result = agent.run_cycle("startup")

    assert result == CycleResult(
        event_id=cast(str, transport.payloads[0]["event_id"]),
        delivered=False,
        spooled=True,
        delivery_kind=kind,
    )
    queued = agent.spool.load(agent.spool.pending()[0])
    assert queued is not None
    assert queued["event_id"] == result.event_id
    assert queued["event"] == "startup"


def test_run_cycle_does_not_spool_permanent_live_rejection(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    transport = FakeTransport([delivery(DeliveryKind.PERMANENT, 422)])
    agent = runtime(tmp_path, transport)

    result = agent.run_cycle("heartbeat")

    assert result.delivery_kind is DeliveryKind.PERMANENT
    assert result.delivered is False
    assert result.spooled is False
    assert agent.spool.pending() == []
    assert result.event_id in caplog.text
    assert "message that must not be logged" not in caplog.text
    assert "token" not in caplog.text


def test_run_cycle_isolates_collector_failure_without_logging_exception(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    transport = FakeTransport([delivery(DeliveryKind.SUCCESS, 200)])
    agent = runtime(tmp_path, transport, collectors=[FailingCollector()])

    result = agent.run_cycle("heartbeat")

    assert result.delivered is True
    collectors = cast(dict[str, JSONValue], transport.payloads[0]["agent"])
    assert "secret process" not in caplog.text
    assert collectors


def test_enqueue_is_followed_by_retention(tmp_path: Path) -> None:
    transport = FakeTransport([delivery(DeliveryKind.RETRIABLE, 503)])
    agent = runtime(tmp_path, transport)
    calls: list[str] = []
    enqueue = agent.spool.enqueue
    enforce_retention = agent.spool.enforce_retention

    def tracked_enqueue(payload: Mapping[str, JSONValue]) -> Path:
        calls.append("enqueue")
        return enqueue(payload)

    def tracked_retention(**kwargs: object) -> object:
        calls.append("retention")
        return enforce_retention(**kwargs)

    agent.spool.enqueue = tracked_enqueue  # type: ignore[method-assign]
    agent.spool.enforce_retention = tracked_retention  # type: ignore[method-assign]

    agent.run_cycle("heartbeat")

    assert calls == ["retention", "enqueue", "retention", "retention"]


def test_run_returns_immediately_when_startup_wait_is_stopped_and_closes(
    tmp_path: Path,
) -> None:
    event = ScriptedEvent([True])
    transport = FakeTransport([])
    agent = runtime(tmp_path, transport, stop_event=event)
    events: list[str] = []
    agent.run_cycle = lambda name: events.append(name)  # type: ignore[method-assign]

    agent.run()

    assert event.waits == [0]
    assert events == []
    assert transport.close_calls == 1


def test_run_executes_exactly_one_startup_cycle_then_stops(tmp_path: Path) -> None:
    event = ScriptedEvent([False])
    transport = FakeTransport([])
    agent = runtime(tmp_path, transport, stop_event=event)
    events: list[str] = []

    def stop_after_startup(name: str) -> None:
        events.append(name)
        event.set()

    agent.run_cycle = stop_after_startup  # type: ignore[method-assign]

    agent.run()

    assert events == ["startup"]
    assert event.waits == [0]
    assert transport.close_calls == 1


def test_run_waits_exact_deadlines_without_busy_loop(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    event = ScriptedEvent([False, False, False, True])
    transport = FakeTransport([])
    agent = runtime(tmp_path, transport, stop_event=event)
    events: list[str] = []
    monotonic = iter([100.0, 100.0, 130.0, 130.0, 160.0, 160.0])
    monkeypatch.setattr("monitor_agent.runtime.time.monotonic", lambda: next(monotonic))
    agent.run_cycle = lambda name: events.append(name)  # type: ignore[method-assign]

    agent.run()

    assert events == ["startup", "heartbeat", "heartbeat"]
    assert event.waits == [0, 30.0, 30.0, 30.0]
    assert transport.close_calls == 1


def test_run_advances_missed_deadlines_without_catch_up_burst(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    event = ScriptedEvent([False, False, True])
    transport = FakeTransport([])
    agent = runtime(tmp_path, transport, stop_event=event)
    events: list[str] = []
    monotonic = iter([100.0, 100.0, 205.0, 205.0])
    monkeypatch.setattr("monitor_agent.runtime.time.monotonic", lambda: next(monotonic))
    agent.run_cycle = lambda name: events.append(name)  # type: ignore[method-assign]

    agent.run()

    assert events == ["startup", "heartbeat"]
    assert event.waits == [0, 30.0, 15.0]


def test_run_checks_stop_before_heartbeat_cycle(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    event = ScriptedEvent([False, False])
    transport = FakeTransport([])
    agent = runtime(tmp_path, transport, stop_event=event)
    events: list[str] = []
    monotonic = iter([100.0, 100.0])
    monkeypatch.setattr("monitor_agent.runtime.time.monotonic", lambda: next(monotonic))

    def wait_and_stop(timeout: float) -> bool:
        event.waits.append(timeout)
        if len(event.waits) == 2:
            event.stopped = True
        return False

    event.wait = wait_and_stop  # type: ignore[method-assign]
    agent.run_cycle = lambda name: events.append(name)  # type: ignore[method-assign]

    agent.run()

    assert events == ["startup"]
    assert transport.close_calls == 1


def test_run_closes_transport_once_and_preserves_cycle_error(tmp_path: Path) -> None:
    transport = RaisingCloseTransport([])
    agent = runtime(tmp_path, transport, stop_event=ScriptedEvent([False]))

    def fail_cycle(name: str) -> None:
        raise ValueError(f"cycle failed: {name}")

    agent.run_cycle = fail_cycle  # type: ignore[method-assign]

    with pytest.raises(ValueError, match="cycle failed: startup"):
        agent.run()
    assert transport.close_calls == 1


def test_run_propagates_close_error_without_a_primary_error(tmp_path: Path) -> None:
    transport = RaisingCloseTransport([])
    agent = runtime(tmp_path, transport, stop_event=ScriptedEvent([True]))

    with pytest.raises(RuntimeError, match="close failed"):
        agent.run()
    assert transport.close_calls == 1


def test_request_stop_is_idempotent(tmp_path: Path) -> None:
    event = ScriptedEvent([])
    agent = runtime(tmp_path, FakeTransport([]), stop_event=event)

    agent.request_stop()
    agent.request_stop()

    assert event.stopped is True
    assert event.set_calls == 1


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("heartbeat_sec", 0, "heartbeat_sec must be positive"),
        ("startup_delay_sec", -1, "startup_delay_sec must be non-negative"),
        ("replay_batch_size", 0, "replay_batch_size must be positive"),
    ],
)
def test_constructor_rejects_invalid_runtime_invariants(
    tmp_path: Path,
    field: str,
    value: int,
    message: str,
) -> None:
    agent_config = replace(config(tmp_path), **{field: value})

    with pytest.raises(ValueError, match=message):
        AgentRuntime(
            config=agent_config,
            identity=MachineIdentity("machine-id", "test"),
            collectors=[EmptyCollector()],
            transport=FakeTransport([]),
            spool=Spool(
                tmp_path,
                agent_config.spool_max_bytes,
                agent_config.spool_max_age_sec,
            ),
            stop_event=Event(),
        )


def test_logs_do_not_contain_spooled_payload(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    transport = FakeTransport([delivery(DeliveryKind.RETRIABLE, 503)])
    agent = runtime(tmp_path, transport)

    result = agent.run_cycle("heartbeat")
    queued = agent.spool.pending()[0]
    raw_payload = json.loads(queued.read_text(encoding="utf-8"))

    assert result.event_id in caplog.text
    assert str(raw_payload) not in caplog.text
    assert "Authorization" not in caplog.text
