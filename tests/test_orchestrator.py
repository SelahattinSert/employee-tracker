from __future__ import annotations

import math
import subprocess
import threading
import time
from concurrent.futures import Future
from concurrent.futures import wait as real_wait
from dataclasses import FrozenInstanceError, fields

import psutil
import pytest

import monitor_agent.orchestrator as orchestrator_module
from monitor_agent.collectors.base import Collector
from monitor_agent.models import (
    CollectionBatch,
    CollectorPayload,
    CollectorResult,
    CollectorStatus,
    JSONValue,
)
from monitor_agent.orchestrator import collect_all


class StaticCollector:
    def __init__(self, name: str, payload: CollectorPayload) -> None:
        self.name = name
        self.payload = payload

    def collect(self) -> CollectorPayload:
        return self.payload


class RaisingCollector:
    def __init__(self, name: str, error: Exception) -> None:
        self.name = name
        self.error = error

    def collect(self) -> CollectorPayload:
        raise self.error


class SleepingCollector:
    def __init__(self, name: str, delay_sec: float) -> None:
        self.name = name
        self.delay_sec = delay_sec

    def collect(self) -> CollectorPayload:
        time.sleep(self.delay_sec)
        return CollectorPayload(data={self.name: True})


class BlockingCollector:
    def __init__(
        self,
        name: str,
        release: threading.Event,
        started: threading.Event | None = None,
    ) -> None:
        self.name = name
        self.release = release
        self.started = started

    def collect(self) -> CollectorPayload:
        if self.started is not None:
            self.started.set()
        self.release.wait(timeout=1.0)
        return CollectorPayload(data={self.name: True})


class EventCollector:
    def __init__(self, name: str, called: threading.Event) -> None:
        self.name = name
        self.called = called

    def collect(self) -> CollectorPayload:
        self.called.set()
        return CollectorPayload(data={self.name: True})


class ConcurrencyTracker:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.active = 0
        self.maximum = 0

    def enter(self) -> None:
        with self.lock:
            self.active += 1
            self.maximum = max(self.maximum, self.active)

    def leave(self) -> None:
        with self.lock:
            self.active -= 1


class TrackedCollector:
    def __init__(self, name: str, tracker: ConcurrencyTracker) -> None:
        self.name = name
        self.tracker = tracker

    def collect(self) -> CollectorPayload:
        self.tracker.enter()
        try:
            time.sleep(0.02)
            return CollectorPayload(data={self.name: True})
        finally:
            self.tracker.leave()


def test_shared_contracts_are_exact_frozen_and_slotted() -> None:
    assert [(status.name, status.value) for status in CollectorStatus] == [
        ("SUCCESS", "success"),
        ("PARTIAL", "partial"),
        ("DISABLED", "disabled"),
        ("TIMED_OUT", "timed_out"),
        ("FAILED", "failed"),
    ]
    assert str(CollectorStatus.SUCCESS) == "success"
    assert [field.name for field in fields(CollectorPayload)] == [
        "data",
        "status",
        "error_code",
        "error_message",
    ]
    assert [field.name for field in fields(CollectorResult)] == [
        "name",
        "status",
        "duration_ms",
        "data",
        "error_code",
        "error_message",
    ]
    assert [field.name for field in fields(CollectionBatch)] == ["results", "duration_ms"]

    nested: JSONValue = {
        "scalars": [None, True, 3, 4.5, "value"],
        "nested": {"items": [{"ready": False}]},
    }
    payload = CollectorPayload(data=nested)
    assert payload.status is CollectorStatus.SUCCESS
    assert payload.error_code is None
    assert payload.error_message is None
    assert not hasattr(payload, "__dict__")
    with pytest.raises(FrozenInstanceError):
        payload.data = {}


def test_collect_all_preserves_registry_order_and_isolates_failure() -> None:
    collectors: list[Collector] = [
        SleepingCollector("first", 0.02),
        RaisingCollector("broken", PermissionError("private path /secret")),
        StaticCollector("last", CollectorPayload(data={"last": 3})),
    ]

    batch = collect_all(collectors, max_workers=3, timeout_sec=1.0)

    assert [result.name for result in batch.results] == ["first", "broken", "last"]
    assert batch.results[0].status is CollectorStatus.SUCCESS
    assert batch.results[1].status is CollectorStatus.FAILED
    assert batch.results[1].error_code == "permission_error"
    assert batch.results[1].error_message == "collector access denied"
    assert "/secret" not in repr(batch.results[1])
    assert batch.results[2].data == {"last": 3}


@pytest.mark.parametrize(
    ("error", "expected_code", "expected_message"),
    [
        (
            PermissionError("private path /secret/permission"),
            "permission_error",
            "collector access denied",
        ),
        (
            psutil.AccessDenied(pid=42, name="/secret/process"),
            "permission_error",
            "collector access denied",
        ),
        (
            subprocess.TimeoutExpired(["private-command", "--token=secret"], 1),
            "timeout",
            "collector timed out",
        ),
        (TimeoutError("private socket /secret/timeout"), "timeout", "collector timed out"),
        (OSError("private path /secret/os"), "os_error", "collector operating system error"),
        (RuntimeError("private argument --token=secret"), "collector_error", "collector failed"),
    ],
)
def test_exception_classification_has_specific_precedence_and_is_sanitized(
    error: Exception,
    expected_code: str,
    expected_message: str,
) -> None:
    batch = collect_all(
        [RaisingCollector("broken", error)],
        max_workers=1,
        timeout_sec=1.0,
    )

    result = batch.results[0]
    assert result.status is CollectorStatus.FAILED
    assert result.error_code == expected_code
    assert result.error_message == expected_message
    assert result.data == {}
    assert "secret" not in repr(result)
    assert "private" not in repr(result)


@pytest.mark.parametrize(
    ("status", "error_code", "error_message"),
    [
        (CollectorStatus.PARTIAL, "disk_access_partial", "some disks unavailable"),
        (CollectorStatus.DISABLED, "disabled_by_policy", "collection disabled"),
        (CollectorStatus.FAILED, "collector_reported_failure", "fixed collector message"),
    ],
)
def test_collector_returned_status_and_error_metadata_are_preserved(
    status: CollectorStatus,
    error_code: str,
    error_message: str,
) -> None:
    payload = CollectorPayload(
        data={"records": []},
        status=status,
        error_code=error_code,
        error_message=error_message,
    )

    result = collect_all(
        [StaticCollector("metadata", payload)],
        max_workers=1,
        timeout_sec=1.0,
    ).results[0]

    assert result.status is status
    assert result.data == {"records": []}
    assert result.error_code == error_code
    assert result.error_message == error_message


def test_collect_all_accepts_an_empty_collector_sequence() -> None:
    batch = collect_all((), max_workers=1, timeout_sec=1.0)

    assert batch.results == ()
    assert isinstance(batch.duration_ms, int)
    assert batch.duration_ms >= 0


@pytest.mark.parametrize("max_workers", [0, -1])
def test_collect_all_rejects_invalid_worker_limits_even_when_empty(max_workers: int) -> None:
    with pytest.raises(ValueError, match="max_workers"):
        collect_all((), max_workers=max_workers, timeout_sec=1.0)


@pytest.mark.parametrize("timeout_sec", [0.0, -1.0, math.inf, math.nan])
def test_collect_all_rejects_invalid_deadlines_even_when_empty(timeout_sec: float) -> None:
    with pytest.raises(ValueError, match="timeout_sec"):
        collect_all((), max_workers=1, timeout_sec=timeout_sec)


def test_collect_all_bounds_simultaneous_execution() -> None:
    tracker = ConcurrencyTracker()
    collectors: list[Collector] = [TrackedCollector(str(index), tracker) for index in range(6)]

    batch = collect_all(collectors, max_workers=2, timeout_sec=1.0)

    assert all(result.status is CollectorStatus.SUCCESS for result in batch.results)
    assert tracker.maximum == 2


def test_each_completed_collector_has_its_own_monotonic_duration() -> None:
    batch = collect_all(
        [SleepingCollector("slower", 0.03), StaticCollector("instant", CollectorPayload({}))],
        max_workers=2,
        timeout_sec=1.0,
    )

    slower, instant = batch.results
    assert slower.duration_ms >= 20
    assert instant.duration_ms < slower.duration_ms
    assert batch.duration_ms >= slower.duration_ms


def test_global_cycle_deadline_applies_to_queued_work() -> None:
    batch = collect_all(
        [SleepingCollector("first", 0.04), SleepingCollector("second", 0.04)],
        max_workers=1,
        timeout_sec=0.06,
    )

    assert batch.results[0].status is CollectorStatus.SUCCESS
    assert batch.results[1].status is CollectorStatus.TIMED_OUT
    assert batch.results[1].data == {}
    assert batch.results[1].error_code == "deadline_exceeded"
    assert batch.duration_ms < 100


def test_queued_collector_is_cancelled_after_deadline() -> None:
    release = threading.Event()
    running_started = threading.Event()
    queued_called = threading.Event()
    try:
        batch = collect_all(
            [
                BlockingCollector("running", release, running_started),
                EventCollector("queued", queued_called),
            ],
            max_workers=1,
            timeout_sec=0.03,
        )
    finally:
        release.set()

    assert running_started.is_set()
    assert not queued_called.wait(timeout=0.05)
    assert [result.status for result in batch.results] == [
        CollectorStatus.TIMED_OUT,
        CollectorStatus.TIMED_OUT,
    ]


def test_future_that_finishes_during_deadline_classification_is_not_dropped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release = threading.Event()
    completed = threading.Event()

    class RacingCollector:
        name = "racing"

        def collect(self) -> CollectorPayload:
            release.wait(timeout=1.0)
            completed.set()
            return CollectorPayload(data={"won_race": True})

    def return_stale_not_done(
        futures: set[Future[CollectorResult]],
        *,
        timeout: float,
    ) -> tuple[set[Future[CollectorResult]], set[Future[CollectorResult]]]:
        done, not_done = real_wait(futures, timeout=0)
        release.set()
        assert completed.wait(timeout=1.0)
        return done, not_done

    monkeypatch.setattr(orchestrator_module, "wait", return_stale_not_done)

    result = collect_all([RacingCollector()], max_workers=1, timeout_sec=0.1).results[0]

    assert result.status is CollectorStatus.SUCCESS
    assert result.data == {"won_race": True}


def test_timeout_returns_without_waiting_for_running_collector() -> None:
    started = time.monotonic()

    batch = collect_all(
        [SleepingCollector("slow", 0.25)],
        max_workers=1,
        timeout_sec=0.02,
    )

    elapsed = time.monotonic() - started
    assert batch.results[0].status is CollectorStatus.TIMED_OUT
    assert batch.results[0].error_message == "collector deadline exceeded"
    assert elapsed < 0.15
