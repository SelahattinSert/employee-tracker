from __future__ import annotations

import math
import subprocess
import threading
import time
from collections.abc import Sequence
from concurrent.futures import Future, ThreadPoolExecutor, wait

import psutil  # type: ignore[import-untyped]

from monitor_agent.collectors.base import Collector
from monitor_agent.models import CollectionBatch, CollectorPayload, CollectorResult, CollectorStatus

_NANOSECONDS_PER_SECOND = 1_000_000_000
_NANOSECONDS_PER_MILLISECOND = 1_000_000

# Collector calls that outlived an earlier cycle deadline. Python cannot stop a running
# thread, so a hung collector is not started again until its previous call returns.
_IN_FLIGHT: dict[int, tuple[Collector, Future[CollectorResult]]] = {}
_IN_FLIGHT_LOCK = threading.Lock()


def _duration_ms(started_ns: int, finished_ns: int) -> int:
    return max(0, (finished_ns - started_ns) // _NANOSECONDS_PER_MILLISECOND)


def _exception_metadata(error: Exception) -> tuple[str, str]:
    if isinstance(error, (PermissionError, psutil.AccessDenied)):
        return "permission_error", "collector access denied"
    if isinstance(error, (subprocess.TimeoutExpired, TimeoutError)):
        return "timeout", "collector timed out"
    if isinstance(error, OSError):
        return "os_error", "collector operating system error"
    return "collector_error", "collector failed"


def _collect_one(name: str, collector: Collector) -> CollectorResult:
    started_ns = time.monotonic_ns()
    try:
        payload: CollectorPayload = collector.collect()
    except Exception as error:
        finished_ns = time.monotonic_ns()
        error_code, error_message = _exception_metadata(error)
        return CollectorResult(
            name=name,
            status=CollectorStatus.FAILED,
            duration_ms=_duration_ms(started_ns, finished_ns),
            data={},
            error_code=error_code,
            error_message=error_message,
        )

    finished_ns = time.monotonic_ns()
    return CollectorResult(
        name=name,
        status=payload.status,
        duration_ms=_duration_ms(started_ns, finished_ns),
        data=payload.data,
        error_code=payload.error_code,
        error_message=payload.error_message,
    )


def _deadline_result(name: str, duration_ms: int) -> CollectorResult:
    return CollectorResult(
        name=name,
        status=CollectorStatus.TIMED_OUT,
        duration_ms=duration_ms,
        data={},
        error_code="deadline_exceeded",
        error_message="collector deadline exceeded",
    )


def _still_running_result(name: str) -> CollectorResult:
    return CollectorResult(
        name=name,
        status=CollectorStatus.TIMED_OUT,
        duration_ms=0,
        data={},
        error_code="collector_still_running",
        error_message="previous collector call has not finished",
    )


def _busy_collector_ids() -> set[int]:
    with _IN_FLIGHT_LOCK:
        return {key for key, (_, future) in _IN_FLIGHT.items() if not future.done()}


def _track(collector: Collector, future: Future[CollectorResult]) -> None:
    key = id(collector)
    with _IN_FLIGHT_LOCK:
        _IN_FLIGHT[key] = (collector, future)

    def forget(done: Future[CollectorResult]) -> None:
        with _IN_FLIGHT_LOCK:
            entry = _IN_FLIGHT.get(key)
            if entry is not None and entry[1] is done:
                del _IN_FLIGHT[key]

    future.add_done_callback(forget)


def _validate_limits(max_workers: int, timeout_sec: float) -> None:
    if max_workers < 1:
        raise ValueError("max_workers must be at least 1")
    if not math.isfinite(timeout_sec) or timeout_sec <= 0:
        raise ValueError("timeout_sec must be finite and greater than 0")


def collect_all(
    collectors: Sequence[Collector],
    *,
    max_workers: int,
    timeout_sec: float,
) -> CollectionBatch:
    _validate_limits(max_workers, timeout_sec)
    cycle_started_ns = time.monotonic_ns()
    registry = tuple(collectors)
    if not registry:
        return CollectionBatch(
            results=(),
            duration_ms=_duration_ms(cycle_started_ns, time.monotonic_ns()),
        )

    deadline_ns = cycle_started_ns + int(timeout_sec * _NANOSECONDS_PER_SECOND)
    executor = ThreadPoolExecutor(
        max_workers=max_workers,
        thread_name_prefix="monitor-agent-collector",
    )
    future_entries: list[tuple[str, Future[CollectorResult] | None]] = []
    busy = _busy_collector_ids()
    try:
        for collector in registry:
            if id(collector) in busy:
                future_entries.append((collector.name, None))
                continue
            future = executor.submit(_collect_one, collector.name, collector)
            _track(collector, future)
            future_entries.append((collector.name, future))
        remaining_sec = max(0.0, (deadline_ns - time.monotonic_ns()) / _NANOSECONDS_PER_SECOND)
        _, possibly_unfinished = wait(
            {future for _, future in future_entries if future is not None},
            timeout=remaining_sec,
        )
        unfinished = {future for future in possibly_unfinished if not future.done()}
        classification_ns = time.monotonic_ns()
        deadline_duration_ms = _duration_ms(cycle_started_ns, classification_ns)
        for future in unfinished:
            future.cancel()

        results = tuple(
            (
                _still_running_result(name)
                if future is None
                else _deadline_result(name, deadline_duration_ms)
                if future in unfinished
                else future.result()
            )
            for name, future in future_entries
        )
    finally:
        executor.shutdown(wait=False, cancel_futures=True)

    return CollectionBatch(
        results=results,
        duration_ms=_duration_ms(cycle_started_ns, time.monotonic_ns()),
    )
