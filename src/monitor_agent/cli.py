from __future__ import annotations

import argparse
import json
import os
import platform
import re
import signal
import sys
import tempfile
import threading
from collections.abc import Sequence
from contextlib import suppress
from pathlib import Path
from types import FrameType

from monitor_agent import __version__
from monitor_agent.collectors import build_collectors
from monitor_agent.config import AgentConfig, ConfigError, load_config
from monitor_agent.identity import resolve_machine_identity
from monitor_agent.logging_setup import configure_logging
from monitor_agent.models import CollectorStatus, JSONValue
from monitor_agent.orchestrator import collect_all
from monitor_agent.payload import build_payload
from monitor_agent.runtime import AgentRuntime
from monitor_agent.spool import Spool
from monitor_agent.transport import TelemetryTransport

_EVENT_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_COLLECTION_FAILURES = {CollectorStatus.FAILED, CollectorStatus.TIMED_OUT}
_PROBE_UNLINK_ATTEMPTS = 3


def _supports_posix_permissions() -> bool:
    return os.name == "posix"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="monitor-agent")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("run", help="run the telemetry agent")
    once = subparsers.add_parser("once", help="collect one telemetry event")
    once.add_argument("--event", default="heartbeat")
    once.add_argument("--no-transmit", action="store_true")
    subparsers.add_parser("check-config", help="validate configuration")
    subparsers.add_parser("health", help="print operational health")
    subparsers.add_parser("version", help="print package and Python versions")
    return parser


def _create_runtime(config: AgentConfig) -> AgentRuntime:
    identity = resolve_machine_identity(config.spool_path.parent)
    return AgentRuntime(
        config=config,
        identity=identity,
        collectors=build_collectors(config, identity),
        transport=TelemetryTransport(config),
        spool=Spool(
            config.spool_path,
            config.spool_max_bytes,
            config.spool_max_age_sec,
        ),
        stop_event=threading.Event(),
    )


def _collect_once(
    config: AgentConfig,
    event: str,
) -> tuple[dict[str, JSONValue], bool]:
    identity = resolve_machine_identity(config.spool_path.parent)
    batch = collect_all(
        build_collectors(config, identity),
        max_workers=config.max_collector_workers,
        timeout_sec=config.collection_timeout_sec,
    )
    payload = build_payload(event, identity, batch)
    aggregate_failure = bool(batch.results) and all(
        item.status in _COLLECTION_FAILURES for item in batch.results
    )
    return payload, aggregate_failure


def _health_snapshot(config: AgentConfig) -> dict[str, JSONValue]:
    identity = resolve_machine_identity(config.spool_path.parent)
    batch = collect_all(
        build_collectors(config, identity),
        max_workers=config.max_collector_workers,
        timeout_sec=config.collection_timeout_sec,
    )
    stats = Spool(
        config.spool_path,
        config.spool_max_bytes,
        config.spool_max_age_sec,
    ).stats()
    collector_statuses: list[JSONValue] = [
        {"name": item.name, "status": item.status.value} for item in batch.results
    ]
    return {
        "version": __version__,
        "python": platform.python_version(),
        "identity_source": identity.source,
        "spool": {
            "pending_count": stats.pending_count,
            "pending_bytes": stats.pending_bytes,
            "dead_letter_count": stats.dead_letter_count,
        },
        "collectors": collector_statuses,
    }


def _validate_paths(config: AgentConfig) -> None:
    _validate_writable_directory(
        config.spool_path,
        "spool path is not writable",
    )

    if config.ca_bundle is not None and (
        not config.ca_bundle.is_file() or not os.access(config.ca_bundle, os.R_OK)
    ):
        raise ConfigError("CA bundle is not readable")

    if config.log_path is None:
        return
    if config.log_path.is_symlink() or (config.log_path.exists() and not config.log_path.is_file()):
        raise ConfigError("log path must identify a file")
    _validate_writable_directory(
        config.log_path.parent,
        "log path parent is not writable",
    )
    if config.log_path.exists():
        _validate_appendable_file(config.log_path)


def _validate_appendable_file(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_APPEND)
    except OSError:
        raise ConfigError("log path is not writable") from None
    try:
        os.close(descriptor)
    except OSError:
        raise ConfigError("log path is not writable") from None


def _validate_writable_directory(path: Path, message: str) -> None:
    probe_path: Path | None = None
    descriptor = -1
    failed = False
    try:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        if _supports_posix_permissions():
            path.chmod(0o700)
        descriptor, probe_name = tempfile.mkstemp(
            prefix=".monitor-agent-probe-",
            dir=path,
        )
        probe_path = Path(probe_name)
        if _supports_posix_permissions():
            os.fchmod(descriptor, 0o600)
    except OSError:
        failed = True
    finally:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                failed = True
        if probe_path is not None:
            for _ in range(_PROBE_UNLINK_ATTEMPTS):
                try:
                    probe_path.unlink()
                    break
                except InterruptedError:
                    continue
                except OSError:
                    failed = True
                    break
            else:
                failed = True
    if failed:
        raise ConfigError(message) from None


def _print_json(value: dict[str, JSONValue]) -> None:
    print(json.dumps(value, separators=(",", ":"), ensure_ascii=False))


def _prepare_config(*, require_transport: bool) -> AgentConfig:
    config = load_config(require_transport=require_transport)
    _validate_paths(config)
    try:
        configure_logging(config)
    except OSError:
        raise ConfigError("log path is not writable") from None
    return config


def _run_command(config: AgentConfig) -> int:
    runtime = _create_runtime(config)

    def request_stop(signum: int, frame: FrameType | None) -> None:
        runtime.request_stop()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    runtime.run()
    return 0


def _once_command(config: AgentConfig, event: str, *, no_transmit: bool) -> int:
    if _EVENT_PATTERN.fullmatch(event) is None:
        print("invalid event name", file=sys.stderr)
        return 2
    if no_transmit:
        try:
            payload, aggregate_failure = _collect_once(config, event)
        except ValueError:
            print("invalid event name", file=sys.stderr)
            return 2
        _print_json(payload)
        return 3 if aggregate_failure else 0

    runtime = _create_runtime(config)
    try:
        result = runtime.run_cycle(event)
    except BaseException:
        with suppress(BaseException):
            runtime.transport.close()
        raise
    else:
        runtime.transport.close()
    return 0 if result.delivered else 4


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "version":
        print(f"monitor-agent {__version__} Python {platform.python_version()}")
        return 0
    try:
        if args.command == "run":
            return _run_command(_prepare_config(require_transport=True))
        if args.command == "once":
            return _once_command(
                _prepare_config(require_transport=not args.no_transmit),
                args.event,
                no_transmit=args.no_transmit,
            )
        if args.command == "check-config":
            _prepare_config(require_transport=True)
            print("configuration valid")
            return 0
        if args.command == "health":
            _print_json(_health_snapshot(_prepare_config(require_transport=True)))
            return 0
    except ConfigError as error:
        print(str(error), file=sys.stderr)
        return 2
    raise AssertionError(f"unhandled command: {args.command}")


def entrypoint() -> None:
    raise SystemExit(main())
