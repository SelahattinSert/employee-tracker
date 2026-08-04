from __future__ import annotations

import json
import os
import platform
import signal
import stat
import tempfile
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from monitor_agent import __version__, cli
from monitor_agent.collectors import build_collectors
from monitor_agent.collectors.base import Collector
from monitor_agent.config import AgentConfig, ConfigError, load_config
from monitor_agent.identity import MachineIdentity
from monitor_agent.models import (
    CollectionBatch,
    CollectorResult,
    CollectorStatus,
    CycleResult,
    DeliveryKind,
    JSONValue,
    SpoolStats,
)


def config(tmp_path: Path, *, transport: bool = True) -> AgentConfig:
    env = {
        "MONITOR_SPOOL_PATH": str(tmp_path / "spool"),
        "MONITOR_LOG_PATH": str(tmp_path / "log" / "agent.log"),
    }
    if transport:
        env.update(
            {
                "MONITOR_COLLECTOR_URI": "https://collector.internal/telemetry",
                "MONITOR_API_TOKEN": "top-secret",
            }
        )
    return load_config(env, require_transport=transport, platform_name="linux")


def result(
    name: str,
    status: CollectorStatus,
) -> CollectorResult:
    return CollectorResult(name, status, 1, {})


def prepare_command(
    monkeypatch: pytest.MonkeyPatch,
    agent_config: AgentConfig,
) -> list[bool]:
    requirements: list[bool] = []

    def fake_load_config(*, require_transport: bool) -> AgentConfig:
        requirements.append(require_transport)
        return agent_config

    monkeypatch.setattr(cli, "load_config", fake_load_config)
    monkeypatch.setattr(cli, "_validate_paths", lambda value: None)
    monkeypatch.setattr(cli, "configure_logging", lambda value: None)
    return requirements


def test_parser_supports_every_command_and_once_options() -> None:
    parser = cli.build_parser()

    assert parser.parse_args(["run"]).command == "run"
    once = parser.parse_args(["once", "--event", "inventory", "--no-transmit"])
    assert once.command == "once"
    assert once.event == "inventory"
    assert once.no_transmit is True
    default_once = parser.parse_args(["once"])
    assert default_once.event == "heartbeat"
    assert default_once.no_transmit is False
    assert parser.parse_args(["check-config"]).command == "check-config"
    assert parser.parse_args(["health"]).command == "health"
    assert parser.parse_args(["version"]).command == "version"


def test_version_prints_package_and_python_without_loading_config(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        cli,
        "load_config",
        lambda **kwargs: pytest.fail("version must not load configuration"),
    )

    assert cli.main(["version"]) == 0

    output = capsys.readouterr().out
    assert __version__ in output
    assert platform.python_version() in output


def test_run_loads_transport_config_and_registers_both_signals(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    requirements = prepare_command(monkeypatch, config(tmp_path))
    runtime = SimpleNamespace(run_calls=0, stop_calls=0)

    def run() -> None:
        runtime.run_calls += 1

    def request_stop() -> None:
        runtime.stop_calls += 1

    runtime.run = run
    runtime.request_stop = request_stop
    monkeypatch.setattr(cli, "_create_runtime", lambda value: runtime)
    registrations: list[tuple[signal.Signals, Callable[..., None]]] = []
    monkeypatch.setattr(
        cli.signal,
        "signal",
        lambda number, handler: registrations.append((number, handler)),
    )

    assert cli.main(["run"]) == 0

    assert requirements == [True]
    assert runtime.run_calls == 1
    assert [number for number, _ in registrations] == [signal.SIGINT, signal.SIGTERM]
    assert registrations[0][1] is registrations[1][1]
    registrations[0][1](signal.SIGINT, None)
    assert runtime.stop_calls == 1


def test_once_no_transmit_loads_optional_transport_and_prints_payload(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    requirements = prepare_command(monkeypatch, config(tmp_path, transport=False))
    payload: dict[str, JSONValue] = {"event": "inventory", "safe": True}
    monkeypatch.setattr(cli, "_collect_once", lambda value, event: (payload, False))
    monkeypatch.setattr(
        cli,
        "TelemetryTransport",
        lambda value: pytest.fail("no-transmit must not construct a transport"),
    )
    monkeypatch.setattr(
        cli.signal,
        "signal",
        lambda *args: pytest.fail("once must not register signals"),
    )

    assert cli.main(["once", "--event", "inventory", "--no-transmit"]) == 0

    assert requirements == [False]
    assert capsys.readouterr().out == '{"event":"inventory","safe":true}\n'


def test_once_no_transmit_returns_aggregate_failure_exit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    prepare_command(monkeypatch, config(tmp_path, transport=False))
    monkeypatch.setattr(cli, "_collect_once", lambda value, event: ({"event": event}, True))

    assert cli.main(["once", "--no-transmit"]) == 3
    assert capsys.readouterr().out == '{"event":"heartbeat"}\n'


@pytest.mark.parametrize(
    ("cycle_result", "expected"),
    [
        (CycleResult("event", True, False, DeliveryKind.SUCCESS), 0),
        (CycleResult("event", False, True, DeliveryKind.RETRIABLE), 4),
        (CycleResult("event", False, False, DeliveryKind.PERMANENT), 4),
    ],
)
def test_transmitting_once_uses_runtime_result_and_closes_transport(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    cycle_result: CycleResult,
    expected: int,
) -> None:
    requirements = prepare_command(monkeypatch, config(tmp_path))
    transport = SimpleNamespace(close_calls=0)
    runtime = SimpleNamespace(transport=transport, events=[])

    def run_cycle(event: str) -> CycleResult:
        runtime.events.append(event)
        return cycle_result

    def close() -> None:
        transport.close_calls += 1

    runtime.run_cycle = run_cycle
    transport.close = close
    monkeypatch.setattr(cli, "_create_runtime", lambda value: runtime)
    monkeypatch.setattr(
        cli.signal,
        "signal",
        lambda *args: pytest.fail("once must not register signals"),
    )

    assert cli.main(["once", "--event", "inventory"]) == expected

    assert requirements == [True]
    assert runtime.events == ["inventory"]
    assert transport.close_calls == 1


def test_transmitting_once_closes_exactly_once_when_cycle_raises(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    prepare_command(monkeypatch, config(tmp_path))
    primary = RuntimeError("cycle failed")
    transport = SimpleNamespace(close_calls=0)
    runtime = SimpleNamespace(transport=transport)

    def run_cycle(event: str) -> CycleResult:
        raise primary

    def close() -> None:
        transport.close_calls += 1

    runtime.run_cycle = run_cycle
    transport.close = close
    monkeypatch.setattr(cli, "_create_runtime", lambda value: runtime)

    with pytest.raises(RuntimeError) as error:
        cli.main(["once"])

    assert error.value is primary
    assert transport.close_calls == 1


def test_transmitting_once_preserves_cycle_error_when_close_also_raises(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    prepare_command(monkeypatch, config(tmp_path))
    primary = RuntimeError("cycle failed")
    transport = SimpleNamespace(close_calls=0)
    runtime = SimpleNamespace(transport=transport)

    def run_cycle(event: str) -> CycleResult:
        raise primary

    def close() -> None:
        transport.close_calls += 1
        raise OSError("close failed")

    runtime.run_cycle = run_cycle
    transport.close = close
    monkeypatch.setattr(cli, "_create_runtime", lambda value: runtime)

    with pytest.raises(RuntimeError) as error:
        cli.main(["once"])

    assert error.value is primary
    assert transport.close_calls == 1


def test_invalid_event_fails_cleanly_without_traceback_or_secret(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    prepare_command(monkeypatch, config(tmp_path, transport=False))
    monkeypatch.setattr(
        cli,
        "_collect_once",
        lambda value, event: (_ for _ in ()).throw(ValueError("raw top-secret details")),
    )

    assert cli.main(["once", "--event", "Not Valid", "--no-transmit"]) == 2

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "invalid event name\n"
    assert "Traceback" not in captured.err
    assert "top-secret" not in captured.err


def test_payload_event_validation_error_is_sanitized(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    prepare_command(monkeypatch, config(tmp_path, transport=False))
    monkeypatch.setattr(
        cli,
        "_collect_once",
        lambda value, event: (_ for _ in ()).throw(ValueError("raw top-secret details")),
    )

    assert cli.main(["once", "--event", "valid-event", "--no-transmit"]) == 2

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "invalid event name\n"


def test_check_config_validates_transport_and_paths_without_work(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    agent_config = config(tmp_path)
    requirements: list[bool] = []
    validations: list[AgentConfig] = []
    monkeypatch.setattr(
        cli,
        "load_config",
        lambda *, require_transport: requirements.append(require_transport) or agent_config,
    )
    monkeypatch.setattr(cli, "_validate_paths", validations.append)
    monkeypatch.setattr(cli, "configure_logging", lambda value: None)
    monkeypatch.setattr(
        cli,
        "_create_runtime",
        lambda value: pytest.fail("check-config must not create runtime"),
    )
    monkeypatch.setattr(
        cli,
        "_collect_once",
        lambda *args: pytest.fail("check-config must not collect"),
    )

    assert cli.main(["check-config"]) == 0

    assert requirements == [True]
    assert validations == [agent_config]
    assert capsys.readouterr().out == "configuration valid\n"


def test_health_loads_transport_config_but_never_constructs_transport(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    requirements = prepare_command(monkeypatch, config(tmp_path))
    snapshot: dict[str, JSONValue] = {"collectors": [], "status": "healthy"}
    monkeypatch.setattr(cli, "_health_snapshot", lambda value: snapshot)
    monkeypatch.setattr(
        cli,
        "TelemetryTransport",
        lambda value: pytest.fail("health must not construct transport"),
    )

    assert cli.main(["health"]) == 0

    assert requirements == [True]
    assert json.loads(capsys.readouterr().out) == snapshot


def test_config_error_boundary_returns_two_and_prints_only_sanitized_message(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        cli,
        "load_config",
        lambda **kwargs: (_ for _ in ()).throw(ConfigError("configuration unavailable")),
    )

    assert cli.main(["run"]) == 2

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "configuration unavailable\n"


def test_validate_paths_creates_owner_only_probe_and_removes_it(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    agent_config = replace(config(tmp_path), log_path=None)
    modes: list[int] = []
    paths: list[Path] = []
    real_mkstemp = tempfile.mkstemp

    def tracked_mkstemp(*args: object, **kwargs: object) -> tuple[int, str]:
        descriptor, name = real_mkstemp(*args, **kwargs)
        paths.append(Path(name))
        modes.append(stat.S_IMODE(os.fstat(descriptor).st_mode))
        return descriptor, name

    monkeypatch.setattr(cli.tempfile, "mkstemp", tracked_mkstemp)

    cli._validate_paths(agent_config)

    assert agent_config.spool_path.is_dir()
    assert modes == [0o600]
    assert paths and all(not path.exists() for path in paths)


def test_validate_paths_creates_and_probes_log_parent(tmp_path: Path) -> None:
    agent_config = config(tmp_path)

    cli._validate_paths(agent_config)

    assert agent_config.log_path is not None
    assert agent_config.log_path.parent.is_dir()
    assert not list(agent_config.log_path.parent.glob(".monitor-agent-probe-*"))


def test_validate_paths_removes_probe_when_permission_check_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    agent_config = replace(config(tmp_path), log_path=None)
    paths: list[Path] = []
    real_mkstemp = tempfile.mkstemp

    def tracked_mkstemp(*args: object, **kwargs: object) -> tuple[int, str]:
        descriptor, name = real_mkstemp(*args, **kwargs)
        paths.append(Path(name))
        return descriptor, name

    monkeypatch.setattr(cli.tempfile, "mkstemp", tracked_mkstemp)
    monkeypatch.setattr(
        cli.os,
        "fchmod",
        lambda descriptor, mode: (_ for _ in ()).throw(PermissionError("raw probe name")),
    )

    with pytest.raises(ConfigError, match="spool path is not writable"):
        cli._validate_paths(agent_config)

    assert paths and all(not path.exists() for path in paths)


def test_validate_paths_checks_ca_and_log_parent_with_sanitized_errors(
    tmp_path: Path,
) -> None:
    missing_ca = replace(config(tmp_path), ca_bundle=tmp_path / "secret-ca-name")
    with pytest.raises(ConfigError, match=r"^CA bundle is not readable$"):
        cli._validate_paths(missing_ca)

    log_directory = tmp_path / "is-a-directory"
    log_directory.mkdir()
    invalid_log = replace(config(tmp_path), ca_bundle=None, log_path=log_directory)
    with pytest.raises(ConfigError, match=r"^log path must identify a file$"):
        cli._validate_paths(invalid_log)


def test_validate_paths_uses_cross_platform_probe_without_posix_mode_calls(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    agent_config = replace(config(tmp_path), log_path=None)
    monkeypatch.setattr(cli, "_supports_posix_permissions", lambda: False)
    monkeypatch.setattr(
        cli.os,
        "fchmod",
        lambda *args: pytest.fail("non-POSIX validation must not call fchmod"),
    )

    cli._validate_paths(agent_config)

    assert agent_config.spool_path.is_dir()


def test_validate_paths_sanitizes_probe_creation_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    agent_config = replace(config(tmp_path), log_path=None)
    monkeypatch.setattr(
        cli.tempfile,
        "mkstemp",
        lambda *args, **kwargs: (_ for _ in ()).throw(PermissionError("raw path and probe")),
    )

    with pytest.raises(ConfigError, match=r"^spool path is not writable$"):
        cli._validate_paths(agent_config)


def test_validate_paths_rejects_existing_log_file_that_cannot_be_appended(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    agent_config = config(tmp_path)
    assert agent_config.log_path is not None
    agent_config.log_path.parent.mkdir(parents=True)
    agent_config.log_path.write_text("existing", encoding="utf-8")
    real_open = os.open

    def fail_log_open(path: str | os.PathLike[str], flags: int, mode: int = 0o777) -> int:
        if os.fspath(path) == os.fspath(agent_config.log_path):
            raise PermissionError("raw private log path")
        return real_open(path, flags, mode)

    monkeypatch.setattr(cli.os, "open", fail_log_open)

    with pytest.raises(ConfigError, match=r"^log path is not writable$") as error:
        cli._validate_paths(agent_config)

    assert "raw private log path" not in str(error.value)
    assert error.value.__cause__ is None


def test_validate_paths_rejects_symlink_log_file(
    tmp_path: Path,
) -> None:
    agent_config = config(tmp_path)
    assert agent_config.log_path is not None
    agent_config.log_path.parent.mkdir(parents=True)
    target = tmp_path / "target.log"
    target.write_text("unchanged", encoding="utf-8")
    agent_config.log_path.symlink_to(target)

    with pytest.raises(ConfigError, match=r"^log path must identify a file$"):
        cli._validate_paths(agent_config)

    assert target.read_text(encoding="utf-8") == "unchanged"


def test_prepare_config_sanitizes_logging_setup_race(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    agent_config = config(tmp_path)
    monkeypatch.setattr(cli, "load_config", lambda **kwargs: agent_config)
    monkeypatch.setattr(cli, "_validate_paths", lambda value: None)
    monkeypatch.setattr(
        cli,
        "configure_logging",
        lambda value: (_ for _ in ()).throw(PermissionError("raw private log path from race")),
    )

    with pytest.raises(ConfigError, match=r"^log path is not writable$") as error:
        cli._prepare_config(require_transport=True)

    assert "raw private log path" not in str(error.value)
    assert error.value.__cause__ is None


def test_validate_paths_reports_unlink_cleanup_failure_without_probe_name(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    agent_config = replace(config(tmp_path), log_path=None)
    probe_paths: list[Path] = []
    real_mkstemp = tempfile.mkstemp
    real_unlink = Path.unlink

    def tracked_mkstemp(*args: object, **kwargs: object) -> tuple[int, str]:
        descriptor, name = real_mkstemp(*args, **kwargs)
        probe_paths.append(Path(name))
        return descriptor, name

    def fail_unlink(path: Path, *, missing_ok: bool = False) -> None:
        raise PermissionError(f"raw probe path {path}")

    monkeypatch.setattr(cli.tempfile, "mkstemp", tracked_mkstemp)
    monkeypatch.setattr(Path, "unlink", fail_unlink)
    try:
        with pytest.raises(ConfigError, match=r"^spool path is not writable$") as error:
            cli._validate_paths(agent_config)
    finally:
        for probe_path in probe_paths:
            if probe_path.exists():
                real_unlink(probe_path)

    assert probe_paths
    assert all(path.name not in str(error.value) for path in probe_paths)
    assert error.value.__cause__ is None


def test_validate_paths_retries_interrupted_probe_unlink(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    agent_config = replace(config(tmp_path), log_path=None)
    probe_paths: list[Path] = []
    unlink_calls = 0
    real_mkstemp = tempfile.mkstemp
    real_unlink = Path.unlink

    def tracked_mkstemp(*args: object, **kwargs: object) -> tuple[int, str]:
        descriptor, name = real_mkstemp(*args, **kwargs)
        probe_paths.append(Path(name))
        return descriptor, name

    def interrupt_once(path: Path, *, missing_ok: bool = False) -> None:
        nonlocal unlink_calls
        unlink_calls += 1
        if unlink_calls == 1:
            raise InterruptedError("raw probe path")
        real_unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(cli.tempfile, "mkstemp", tracked_mkstemp)
    monkeypatch.setattr(Path, "unlink", interrupt_once)

    cli._validate_paths(agent_config)

    assert unlink_calls == 2
    assert probe_paths and all(not path.exists() for path in probe_paths)


def test_validate_paths_caps_repeated_interrupted_probe_unlink(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    agent_config = replace(config(tmp_path), log_path=None)
    probe_paths: list[Path] = []
    unlink_calls = 0
    real_mkstemp = tempfile.mkstemp
    real_unlink = Path.unlink

    def tracked_mkstemp(*args: object, **kwargs: object) -> tuple[int, str]:
        descriptor, name = real_mkstemp(*args, **kwargs)
        probe_paths.append(Path(name))
        return descriptor, name

    def always_interrupted(path: Path, *, missing_ok: bool = False) -> None:
        nonlocal unlink_calls
        unlink_calls += 1
        if unlink_calls > 3:
            raise RuntimeError("unlink retry was not capped")
        raise InterruptedError("raw probe path")

    monkeypatch.setattr(cli.tempfile, "mkstemp", tracked_mkstemp)
    monkeypatch.setattr(Path, "unlink", always_interrupted)
    try:
        with pytest.raises(ConfigError, match=r"^spool path is not writable$") as error:
            cli._validate_paths(agent_config)
    finally:
        for probe_path in probe_paths:
            if probe_path.exists():
                real_unlink(probe_path)

    assert unlink_calls == 3
    assert error.value.__cause__ is None


def test_validate_paths_attempts_unlink_after_primary_and_close_failures(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    agent_config = replace(config(tmp_path), log_path=None)
    probe_paths: list[Path] = []
    unlinked_paths: list[Path] = []
    real_mkstemp = tempfile.mkstemp
    real_close = os.close
    real_unlink = Path.unlink

    def tracked_mkstemp(*args: object, **kwargs: object) -> tuple[int, str]:
        descriptor, name = real_mkstemp(*args, **kwargs)
        probe_paths.append(Path(name))
        return descriptor, name

    def fail_fchmod(descriptor: int, mode: int) -> None:
        raise PermissionError("raw primary probe failure")

    def close_then_fail(descriptor: int) -> None:
        real_close(descriptor)
        raise OSError("raw close failure")

    def tracked_unlink(path: Path, *, missing_ok: bool = False) -> None:
        unlinked_paths.append(path)
        real_unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(cli.tempfile, "mkstemp", tracked_mkstemp)
    monkeypatch.setattr(cli.os, "fchmod", fail_fchmod)
    monkeypatch.setattr(cli.os, "close", close_then_fail)
    monkeypatch.setattr(Path, "unlink", tracked_unlink)

    with pytest.raises(ConfigError, match=r"^spool path is not writable$") as error:
        cli._validate_paths(agent_config)

    assert probe_paths == unlinked_paths
    assert all(not path.exists() for path in probe_paths)
    assert error.value.__cause__ is None


def test_build_collectors_uses_fixed_order_and_configuration(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import monitor_agent.collectors as registry

    calls: list[tuple[str, object]] = []

    def factory(name: str) -> Callable[..., Collector]:
        def create(*arguments: object) -> Collector:
            argument: object
            if not arguments:
                argument = None
            elif len(arguments) == 1:
                argument = arguments[0]
            else:
                argument = arguments
            calls.append((name, argument))
            return cast(Collector, SimpleNamespace(name=name))

        return create

    monkeypatch.setattr(registry, "SystemCollector", factory("system"))
    monkeypatch.setattr(registry, "UsersCollector", factory("users"))
    monkeypatch.setattr(registry, "ResourceCollector", factory("resources"))
    monkeypatch.setattr(registry, "NetworkCollector", factory("network"))
    monkeypatch.setattr(registry, "ProcessesCollector", factory("processes"))
    monkeypatch.setattr(registry, "SoftwareCollector", factory("software"))
    monkeypatch.setattr(registry, "ActiveWindowCollector", factory("active_window"))
    monkeypatch.setattr(registry, "FileAuditCollector", factory("file_audit"))
    monkeypatch.setattr(registry, "ScreenshotCollector", factory("screenshot"))
    identity = MachineIdentity("machine-value", "test-source")
    agent_config = replace(
        config(tmp_path),
        include_network_connections=False,
        process_cmdline_mode="none",
        include_software=False,
    )

    collectors = build_collectors(agent_config, identity)

    assert [collector.name for collector in collectors] == [
        "system",
        "users",
        "resources",
        "network",
        "processes",
        "software",
        "active_window",
        "file_audit",
        "screenshot",
    ]
    assert calls == [
        ("system", identity),
        ("users", None),
        ("resources", None),
        ("network", False),
        ("processes", "none"),
        ("software", False),
        ("active_window", False),
        (
            "file_audit",
            ((), 50, 10485760),
        ),
        (
            "screenshot",
            (False, 5242880),
        ),
    ]


@pytest.mark.parametrize(
    ("statuses", "aggregate_failure"),
    [
        ([CollectorStatus.FAILED, CollectorStatus.TIMED_OUT], True),
        ([CollectorStatus.FAILED, CollectorStatus.DISABLED], False),
        ([CollectorStatus.SUCCESS, CollectorStatus.TIMED_OUT], False),
    ],
)
def test_collect_once_builds_payload_and_classifies_aggregate_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    statuses: list[CollectorStatus],
    aggregate_failure: bool,
) -> None:
    agent_config = config(tmp_path, transport=False)
    identity = MachineIdentity("private-machine", "test-source")
    collectors = cast(list[Collector], [SimpleNamespace(name="one")])
    batch = CollectionBatch(
        tuple(result(f"collector-{index}", status) for index, status in enumerate(statuses)),
        10,
    )
    payload: dict[str, JSONValue] = {"event": "inventory"}
    monkeypatch.setattr(cli, "resolve_machine_identity", lambda path: identity)
    monkeypatch.setattr(cli, "build_collectors", lambda value, machine: collectors)
    monkeypatch.setattr(cli, "collect_all", lambda *args, **kwargs: batch)
    monkeypatch.setattr(
        cli,
        "build_payload",
        lambda event, machine, collected: payload,
    )

    assert cli._collect_once(agent_config, "inventory") == (payload, aggregate_failure)


def test_health_snapshot_contains_only_safe_deterministic_operational_data(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    agent_config = config(tmp_path)
    monkeypatch.setattr(
        cli,
        "resolve_machine_identity",
        lambda path: MachineIdentity("raw-private-machine", "linux-machine-id"),
    )
    collectors = cast(
        list[Collector],
        [SimpleNamespace(name="system"), SimpleNamespace(name="users")],
    )
    monkeypatch.setattr(cli, "build_collectors", lambda value, identity: collectors)
    monkeypatch.setattr(
        cli,
        "collect_all",
        lambda *args, **kwargs: CollectionBatch(
            (
                result("system", CollectorStatus.SUCCESS),
                result("users", CollectorStatus.FAILED),
            ),
            9,
        ),
    )

    class FakeSpool:
        def __init__(self, root: Path, max_bytes: int, max_age_sec: int) -> None:
            pass

        def stats(self) -> SpoolStats:
            return SpoolStats(2, 300, 1)

    monkeypatch.setattr(cli, "Spool", FakeSpool)

    snapshot = cli._health_snapshot(agent_config)
    encoded = json.dumps(snapshot)

    assert snapshot == {
        "version": __version__,
        "python": platform.python_version(),
        "identity_source": "linux-machine-id",
        "spool": {
            "pending_count": 2,
            "pending_bytes": 300,
            "dead_letter_count": 1,
        },
        "collectors": [
            {"name": "system", "status": "success"},
            {"name": "users", "status": "failed"},
        ],
    }
    assert "raw-private-machine" not in encoded
    assert "top-secret" not in encoded


def test_create_runtime_wires_accepted_components(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    agent_config = config(tmp_path)
    identity = MachineIdentity("private", "source")
    collectors = cast(list[Collector], [SimpleNamespace(name="system")])
    transport = SimpleNamespace()
    spool = SimpleNamespace()
    stop_event = SimpleNamespace()
    calls: dict[str, object] = {}
    monkeypatch.setattr(cli, "resolve_machine_identity", lambda path: identity)
    monkeypatch.setattr(cli, "build_collectors", lambda value, machine: collectors)
    monkeypatch.setattr(cli, "TelemetryTransport", lambda value: transport)
    monkeypatch.setattr(cli, "Spool", lambda *args: spool)
    monkeypatch.setattr(cli.threading, "Event", lambda: stop_event)

    class FakeRuntime:
        def __init__(self, **kwargs: object) -> None:
            calls.update(kwargs)

    monkeypatch.setattr(cli, "AgentRuntime", FakeRuntime)

    created = cli._create_runtime(agent_config)

    assert isinstance(created, FakeRuntime)
    assert calls == {
        "config": agent_config,
        "identity": identity,
        "collectors": collectors,
        "transport": transport,
        "spool": spool,
        "stop_event": stop_event,
    }
