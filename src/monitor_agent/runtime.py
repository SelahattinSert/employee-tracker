"""Durable telemetry delivery and monotonic runtime scheduling."""

from __future__ import annotations

import logging
import time
from collections.abc import Mapping, Sequence
from contextlib import suppress
from typing import Protocol, cast

from monitor_agent.collectors.base import Collector
from monitor_agent.config import AgentConfig
from monitor_agent.identity import MachineIdentity
from monitor_agent.models import CycleResult, DeliveryKind, DeliveryResult, JSONValue
from monitor_agent.orchestrator import collect_all
from monitor_agent.payload import build_payload
from monitor_agent.spool import Spool

logger = logging.getLogger(__name__)


class Transport(Protocol):
    def send(self, payload: Mapping[str, JSONValue]) -> DeliveryResult: ...

    def close(self) -> None: ...


class StopEvent(Protocol):
    def wait(self, timeout: float) -> bool: ...

    def set(self) -> None: ...

    def is_set(self) -> bool: ...


class AgentRuntime:
    """Coordinate collection, ordered replay, delivery, and shutdown."""

    def __init__(
        self,
        *,
        config: AgentConfig,
        identity: MachineIdentity,
        collectors: Sequence[Collector],
        transport: Transport,
        spool: Spool,
        stop_event: StopEvent,
    ) -> None:
        if config.heartbeat_sec <= 0:
            raise ValueError("heartbeat_sec must be positive")
        if config.startup_delay_sec < 0:
            raise ValueError("startup_delay_sec must be non-negative")
        if config.replay_batch_size <= 0:
            raise ValueError("replay_batch_size must be positive")

        self.config = config
        self.identity = identity
        self.collectors = tuple(collectors)
        self.transport = transport
        self.spool = spool
        self.stop_event = stop_event

    def replay(self) -> bool:
        """Replay one bounded oldest-first batch and report whether the queue is empty."""
        records = self.spool.pending()[: self.config.replay_batch_size]
        try:
            for record in records:
                payload = self.spool.load(record)
                if payload is None:
                    logger.warning("delivery kind=corrupt status=dead_letter")
                    continue

                result = self.transport.send(payload)
                event_id = cast(str, payload["event_id"])
                self._log_delivery(event_id, result)
                if result.kind is DeliveryKind.SUCCESS:
                    self.spool.ack(record)
                    continue
                if result.kind is DeliveryKind.PERMANENT:
                    self.spool.reject(record)
                    continue
                break
        finally:
            self.spool.enforce_retention()

        return not self.spool.pending()

    def run_cycle(self, event: str) -> CycleResult:
        """Build one live event, replay backlog, then deliver or durably queue it."""
        batch = collect_all(
            self.collectors,
            max_workers=self.config.max_collector_workers,
            timeout_sec=self.config.collection_timeout_sec,
        )
        payload = build_payload(event, self.identity, batch)
        event_id = cast(str, payload["event_id"])

        if not self.replay():
            self._enqueue(payload)
            logger.warning(
                "delivery event_id=%s kind=deferred status=spooled",
                event_id,
            )
            return CycleResult(event_id, False, True, None)

        result = self.transport.send(payload)
        self._log_delivery(event_id, result)
        if result.kind is DeliveryKind.SUCCESS:
            return CycleResult(event_id, True, False, result.kind)
        if result.kind in (DeliveryKind.RETRIABLE, DeliveryKind.AUTHENTICATION):
            self._enqueue(payload)
            return CycleResult(event_id, False, True, result.kind)
        return CycleResult(event_id, False, False, result.kind)

    def run(self) -> None:
        """Run startup and heartbeat cycles until shutdown is requested."""
        try:
            self._run_loop()
        except BaseException:
            with suppress(BaseException):
                self.transport.close()
            raise
        else:
            self.transport.close()

    def request_stop(self) -> None:
        """Request an interruptible, idempotent runtime shutdown."""
        if not self.stop_event.is_set():
            self.stop_event.set()

    def _run_loop(self) -> None:
        if self.stop_event.is_set():
            return
        if self.stop_event.wait(float(self.config.startup_delay_sec)):
            return
        if self.stop_event.is_set():
            return

        self.run_cycle("startup")
        if self.stop_event.is_set():
            return

        interval = float(self.config.heartbeat_sec)
        next_deadline = time.monotonic() + interval
        while True:
            remaining = max(0.0, next_deadline - time.monotonic())
            if self.stop_event.wait(remaining):
                return
            if self.stop_event.is_set():
                return

            self.run_cycle("heartbeat")
            if self.stop_event.is_set():
                return

            now = time.monotonic()
            while next_deadline <= now:
                next_deadline += interval

    def _enqueue(self, payload: Mapping[str, JSONValue]) -> None:
        self.spool.enqueue(payload)
        self.spool.enforce_retention()

    @staticmethod
    def _log_delivery(event_id: str, result: DeliveryResult) -> None:
        if result.kind is DeliveryKind.SUCCESS:
            return
        logger.warning(
            "delivery event_id=%s kind=%s status=%s",
            event_id,
            result.kind.value,
            "none" if result.status_code is None else result.status_code,
        )
