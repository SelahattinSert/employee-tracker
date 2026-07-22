import sys
from dataclasses import FrozenInstanceError, fields
from pathlib import Path

import pytest

from monitor_agent.config import AgentConfig, ConfigError, load_config

BASE_ENV = {
    "MONITOR_COLLECTOR_URI": "https://collector.internal/api/v1/telemetry",
    "MONITOR_API_TOKEN": "secret-token",
}


def test_load_config_defaults() -> None:
    config = load_config(BASE_ENV, platform_name="linux")

    assert config.collector_uri == BASE_ENV["MONITOR_COLLECTOR_URI"]
    assert config.api_token == BASE_ENV["MONITOR_API_TOKEN"]
    assert config.heartbeat_sec == 300
    assert config.startup_delay_sec == 30
    assert config.connect_timeout_sec == 5.0
    assert config.read_timeout_sec == 15.0
    assert config.collection_timeout_sec == 30.0
    assert config.max_collector_workers == 4
    assert config.spool_path == Path("/var/lib/monitor-agent/spool")
    assert config.spool_max_bytes == 104857600
    assert config.spool_max_age_sec == 604800
    assert config.replay_batch_size == 20
    assert config.ca_bundle is None
    assert config.process_cmdline_mode == "redacted"
    assert config.include_network_connections is True
    assert config.include_software is True
    assert config.log_path is None
    assert config.log_format == "text"
    assert config.log_level == "INFO"


def test_agent_config_is_frozen_slotted_and_has_exact_fields() -> None:
    config = load_config(BASE_ENV, platform_name="linux")
    assert [field.name for field in fields(AgentConfig)] == [
        "collector_uri",
        "api_token",
        "heartbeat_sec",
        "startup_delay_sec",
        "connect_timeout_sec",
        "read_timeout_sec",
        "collection_timeout_sec",
        "max_collector_workers",
        "spool_path",
        "spool_max_bytes",
        "spool_max_age_sec",
        "replay_batch_size",
        "ca_bundle",
        "process_cmdline_mode",
        "include_network_connections",
        "include_software",
        "log_path",
        "log_format",
        "log_level",
    ]
    assert not hasattr(config, "__dict__")
    with pytest.raises(FrozenInstanceError):
        config.heartbeat_sec = 60


def test_agent_config_repr_omits_api_token() -> None:
    token = "real-secret-token"

    config = load_config(
        BASE_ENV | {"MONITOR_API_TOKEN": token}, platform_name="linux"
    )

    assert token not in repr(config)


@pytest.mark.parametrize(
    ("name", "value", "message"),
    [
        ("MONITOR_COLLECTOR_URI", "http://collector.internal/telemetry", "must use HTTPS"),
        ("MONITOR_HEARTBEAT_SEC", "29", "between 30 and 86400"),
        ("MONITOR_MAX_COLLECTOR_WORKERS", "0", "between 1 and 32"),
        ("MONITOR_PROCESS_CMDLINE_MODE", "raw", "none, redacted, or full"),
        ("MONITOR_INCLUDE_SOFTWARE", "sometimes", "true or false"),
    ],
)
def test_invalid_values_are_rejected(name: str, value: str, message: str) -> None:
    env = BASE_ENV | {name: value}
    with pytest.raises(ConfigError, match=message):
        load_config(env, platform_name="linux")


@pytest.mark.parametrize(
    ("name", "minimum", "maximum"),
    [
        ("MONITOR_HEARTBEAT_SEC", "30", "86400"),
        ("MONITOR_STARTUP_DELAY_SEC", "0", "3600"),
        ("MONITOR_CONNECT_TIMEOUT_SEC", "0.1", "300.0"),
        ("MONITOR_READ_TIMEOUT_SEC", "0.1", "300.0"),
        ("MONITOR_COLLECTION_TIMEOUT_SEC", "1.0", "3600.0"),
        ("MONITOR_MAX_COLLECTOR_WORKERS", "1", "32"),
        ("MONITOR_SPOOL_MAX_BYTES", "1048576", "10737418240"),
        ("MONITOR_SPOOL_MAX_AGE_SEC", "3600", "31536000"),
        ("MONITOR_REPLAY_BATCH_SIZE", "1", "1000"),
    ],
)
def test_numeric_boundaries_are_accepted(name: str, minimum: str, maximum: str) -> None:
    assert load_config(BASE_ENV | {name: minimum}, platform_name="linux")
    assert load_config(BASE_ENV | {name: maximum}, platform_name="linux")


@pytest.mark.parametrize(
    ("name", "value", "message"),
    [
        ("MONITOR_HEARTBEAT_SEC", "30.5", "must be an integer"),
        ("MONITOR_STARTUP_DELAY_SEC", "3601", "between 0 and 3600"),
        ("MONITOR_CONNECT_TIMEOUT_SEC", "fast", "must be a number"),
        ("MONITOR_READ_TIMEOUT_SEC", "nan", "between 0.1 and 300.0"),
        ("MONITOR_COLLECTION_TIMEOUT_SEC", "0.9", "between 1.0 and 3600.0"),
        ("MONITOR_MAX_COLLECTOR_WORKERS", "33", "between 1 and 32"),
        ("MONITOR_SPOOL_MAX_BYTES", "1048575", "between 1048576 and 10737418240"),
        ("MONITOR_SPOOL_MAX_AGE_SEC", "31536001", "between 3600 and 31536000"),
        ("MONITOR_REPLAY_BATCH_SIZE", "0", "between 1 and 1000"),
    ],
)
def test_invalid_numeric_settings_are_rejected(name: str, value: str, message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        load_config(BASE_ENV | {name: value}, platform_name="linux")


@pytest.mark.parametrize("value", ["true", "TRUE", "1", "yes", "YeS"])
def test_true_boolean_spellings_are_accepted(value: str) -> None:
    config = load_config(
        BASE_ENV | {"MONITOR_INCLUDE_NETWORK_CONNECTIONS": value}, platform_name="linux"
    )
    assert config.include_network_connections is True


@pytest.mark.parametrize("value", ["false", "FALSE", "0", "no", "No"])
def test_false_boolean_spellings_are_accepted(value: str) -> None:
    config = load_config(BASE_ENV | {"MONITOR_INCLUDE_SOFTWARE": value}, platform_name="linux")
    assert config.include_software is False


@pytest.mark.parametrize(
    ("uri", "message"),
    [
        ("https:///telemetry", "must include a hostname"),
        ("https://user@collector.internal/telemetry", "must not include credentials"),
        ("https://user:password@collector.internal/telemetry", "must not include credentials"),
        ("https://[invalid/telemetry", "must be a valid HTTPS URI"),
    ],
)
def test_invalid_collector_uris_are_rejected(uri: str, message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        load_config(BASE_ENV | {"MONITOR_COLLECTOR_URI": uri}, platform_name="linux")


@pytest.mark.parametrize(
    "uri",
    [
        "https://collector.internal:notaport/telemetry",
        "https://collector.internal:70000/telemetry",
    ],
)
def test_invalid_collector_ports_are_rejected(uri: str) -> None:
    with pytest.raises(ConfigError, match="must be a valid HTTPS URI"):
        load_config(BASE_ENV | {"MONITOR_COLLECTOR_URI": uri}, platform_name="linux")


def test_all_overrides_are_loaded(tmp_path: Path) -> None:
    ca_bundle = tmp_path / "ca.pem"
    ca_bundle.write_text("test CA", encoding="utf-8")
    config = load_config(
        {
            "MONITOR_COLLECTOR_URI": "https://collector.example:8443/telemetry",
            "MONITOR_API_TOKEN": "another-token",
            "MONITOR_HEARTBEAT_SEC": "60",
            "MONITOR_STARTUP_DELAY_SEC": "0",
            "MONITOR_CONNECT_TIMEOUT_SEC": "1.5",
            "MONITOR_READ_TIMEOUT_SEC": "2.5",
            "MONITOR_COLLECTION_TIMEOUT_SEC": "10.5",
            "MONITOR_MAX_COLLECTOR_WORKERS": "8",
            "MONITOR_SPOOL_PATH": "/tmp/custom-spool",
            "MONITOR_SPOOL_MAX_BYTES": "2097152",
            "MONITOR_SPOOL_MAX_AGE_SEC": "7200",
            "MONITOR_REPLAY_BATCH_SIZE": "50",
            "MONITOR_CA_BUNDLE": str(ca_bundle),
            "MONITOR_PROCESS_CMDLINE_MODE": "full",
            "MONITOR_INCLUDE_NETWORK_CONNECTIONS": "no",
            "MONITOR_INCLUDE_SOFTWARE": "0",
            "MONITOR_LOG_PATH": "/tmp/monitor-agent.log",
            "MONITOR_LOG_FORMAT": "json",
            "MONITOR_LOG_LEVEL": "debug",
        },
        platform_name="linux",
    )

    assert config == AgentConfig(
        collector_uri="https://collector.example:8443/telemetry",
        api_token="another-token",
        heartbeat_sec=60,
        startup_delay_sec=0,
        connect_timeout_sec=1.5,
        read_timeout_sec=2.5,
        collection_timeout_sec=10.5,
        max_collector_workers=8,
        spool_path=Path("/tmp/custom-spool"),
        spool_max_bytes=2097152,
        spool_max_age_sec=7200,
        replay_batch_size=50,
        ca_bundle=ca_bundle,
        process_cmdline_mode="full",
        include_network_connections=False,
        include_software=False,
        log_path=Path("/tmp/monitor-agent.log"),
        log_format="json",
        log_level="DEBUG",
    )


@pytest.mark.parametrize(
    ("platform_name", "spool_path", "log_path"),
    [
        ("linux", Path("/var/lib/monitor-agent/spool"), None),
        (
            "win32",
            Path(r"C:\ProgramData\MonitorAgent\spool"),
            Path(r"C:\ProgramData\MonitorAgent\logs\monitor-agent.log"),
        ),
        (
            "darwin",
            Path("/Library/Application Support/MonitorAgent/spool"),
            Path("/Library/Logs/MonitorAgent/monitor-agent.log"),
        ),
    ],
)
def test_platform_paths(
    platform_name: str, spool_path: Path, log_path: Path | None
) -> None:
    config = load_config({}, require_transport=False, platform_name=platform_name)
    assert config.spool_path == spool_path
    assert config.log_path == log_path


def test_platform_defaults_to_sys_platform(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MONITOR_COLLECTOR_URI", BASE_ENV["MONITOR_COLLECTOR_URI"])
    monkeypatch.setenv("MONITOR_API_TOKEN", BASE_ENV["MONITOR_API_TOKEN"])

    config = load_config()

    expected = Path(r"C:\ProgramData\MonitorAgent\spool") if sys.platform == "win32" else None
    if expected is None:
        expected = (
            Path("/Library/Application Support/MonitorAgent/spool")
            if sys.platform == "darwin"
            else Path("/var/lib/monitor-agent/spool")
        )
    assert config.spool_path == expected


def test_no_transmit_mode_allows_missing_transport_values() -> None:
    config = load_config({}, require_transport=False, platform_name="win32")
    assert config.collector_uri is None
    assert config.api_token is None
    assert config.spool_path == Path(r"C:\ProgramData\MonitorAgent\spool")


@pytest.mark.parametrize(
    "env",
    [
        {},
        {"MONITOR_API_TOKEN": "do-not-print"},
        {"MONITOR_COLLECTOR_URI": "https://collector.internal/telemetry"},
    ],
)
def test_transport_mode_requires_both_transport_values(env: dict[str, str]) -> None:
    with pytest.raises(ConfigError):
        load_config(env, platform_name="linux")


def test_transport_mode_never_echoes_token() -> None:
    with pytest.raises(ConfigError) as error:
        load_config({"MONITOR_API_TOKEN": "do-not-print"}, platform_name="linux")
    assert "do-not-print" not in str(error.value)


def test_non_transmit_mode_still_validates_supplied_uri() -> None:
    with pytest.raises(ConfigError, match="must use HTTPS"):
        load_config(
            {"MONITOR_COLLECTOR_URI": "http://collector.internal/telemetry"},
            require_transport=False,
            platform_name="linux",
        )


@pytest.mark.parametrize("path_kind", ["missing", "directory"])
def test_ca_bundle_must_be_an_existing_regular_file(tmp_path: Path, path_kind: str) -> None:
    ca_bundle = tmp_path / "ca"
    if path_kind == "directory":
        ca_bundle.mkdir()

    with pytest.raises(ConfigError, match="existing regular file"):
        load_config(BASE_ENV | {"MONITOR_CA_BUNDLE": str(ca_bundle)}, platform_name="linux")


@pytest.mark.parametrize("mode", ["none", "redacted", "full"])
def test_process_cmdline_modes_are_accepted(mode: str) -> None:
    config = load_config(
        BASE_ENV | {"MONITOR_PROCESS_CMDLINE_MODE": mode}, platform_name="linux"
    )
    assert config.process_cmdline_mode == mode


def test_invalid_log_format_is_rejected() -> None:
    with pytest.raises(ConfigError, match="text or json"):
        load_config(BASE_ENV | {"MONITOR_LOG_FORMAT": "structured"}, platform_name="linux")
