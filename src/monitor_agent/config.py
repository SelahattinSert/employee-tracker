from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, TypeAlias, cast
from urllib.parse import urlsplit

ProcessCmdlineMode: TypeAlias = Literal["none", "redacted", "full"]


class ConfigError(ValueError):
    """Raised when an environment setting cannot produce a safe configuration."""


@dataclass(frozen=True, slots=True)
class AgentConfig:
    collector_uri: str | None
    api_token: str | None = field(repr=False)
    heartbeat_sec: int
    startup_delay_sec: int
    connect_timeout_sec: float
    read_timeout_sec: float
    collection_timeout_sec: float
    max_collector_workers: int
    spool_path: Path
    spool_max_bytes: int
    spool_max_age_sec: int
    replay_batch_size: int
    ca_bundle: Path | None
    process_cmdline_mode: ProcessCmdlineMode
    include_network_connections: bool
    include_software: bool
    log_path: Path | None
    log_format: Literal["text", "json"]
    log_level: str


def _parse_int(env: Mapping[str, str], name: str, default: int, minimum: int, maximum: int) -> int:
    raw_value = env.get(name)
    if raw_value is None:
        return default
    try:
        value = int(raw_value)
    except ValueError as error:
        raise ConfigError(f"{name} must be an integer") from error
    if not minimum <= value <= maximum:
        raise ConfigError(f"{name} must be between {minimum} and {maximum}")
    return value


def _parse_float(
    env: Mapping[str, str], name: str, default: float, minimum: float, maximum: float
) -> float:
    raw_value = env.get(name)
    if raw_value is None:
        return default
    try:
        value = float(raw_value)
    except ValueError as error:
        raise ConfigError(f"{name} must be a number") from error
    if not minimum <= value <= maximum:
        raise ConfigError(f"{name} must be between {minimum} and {maximum}")
    return value


def _parse_bool(env: Mapping[str, str], name: str, default: bool) -> bool:
    raw_value = env.get(name)
    if raw_value is None:
        return default
    normalized = raw_value.casefold()
    if normalized in {"true", "1", "yes"}:
        return True
    if normalized in {"false", "0", "no"}:
        return False
    raise ConfigError(f"{name} must be true or false (also accepted: 1/0 or yes/no)")


def _default_spool_path(platform_name: str) -> Path:
    if platform_name == "win32":
        return Path(r"C:\ProgramData\MonitorAgent\spool")
    if platform_name == "darwin":
        return Path("/Library/Application Support/MonitorAgent/spool")
    return Path("/var/lib/monitor-agent/spool")


def _default_log_path(platform_name: str) -> Path | None:
    if platform_name == "win32":
        return Path(r"C:\ProgramData\MonitorAgent\logs\monitor-agent.log")
    if platform_name == "darwin":
        return Path("/Library/Logs/MonitorAgent/monitor-agent.log")
    return None


def _validate_collector_uri(uri: str) -> None:
    try:
        parsed = urlsplit(uri)
        hostname = parsed.hostname
        username = parsed.username
        password = parsed.password
        _ = parsed.port
    except ValueError as error:
        raise ConfigError("MONITOR_COLLECTOR_URI must be a valid HTTPS URI") from error
    if parsed.scheme.casefold() != "https":
        raise ConfigError("MONITOR_COLLECTOR_URI must use HTTPS")
    if not hostname:
        raise ConfigError("MONITOR_COLLECTOR_URI must include a hostname")
    if username is not None or password is not None:
        raise ConfigError("MONITOR_COLLECTOR_URI must not include credentials")


def load_config(
    env: Mapping[str, str] | None = None,
    *,
    require_transport: bool = True,
    platform_name: str | None = None,
) -> AgentConfig:
    source = os.environ if env is None else env
    platform = sys.platform if platform_name is None else platform_name

    collector_uri = source.get("MONITOR_COLLECTOR_URI") or None
    api_token = source.get("MONITOR_API_TOKEN") or None
    if collector_uri is not None:
        _validate_collector_uri(collector_uri)
    if require_transport and collector_uri is None:
        raise ConfigError("MONITOR_COLLECTOR_URI is required for transport")
    if require_transport and api_token is None:
        raise ConfigError("MONITOR_API_TOKEN is required for transport")

    ca_bundle_value = source.get("MONITOR_CA_BUNDLE")
    ca_bundle = Path(ca_bundle_value) if ca_bundle_value else None
    if ca_bundle is not None and not ca_bundle.is_file():
        raise ConfigError("MONITOR_CA_BUNDLE must identify an existing regular file")

    process_cmdline_value = source.get("MONITOR_PROCESS_CMDLINE_MODE", "redacted")
    if process_cmdline_value not in {"none", "redacted", "full"}:
        raise ConfigError("MONITOR_PROCESS_CMDLINE_MODE must be none, redacted, or full")
    process_cmdline_mode = cast(ProcessCmdlineMode, process_cmdline_value)

    log_format_value = source.get("MONITOR_LOG_FORMAT", "text")
    if log_format_value not in {"text", "json"}:
        raise ConfigError("MONITOR_LOG_FORMAT must be text or json")
    log_format = cast(Literal["text", "json"], log_format_value)

    spool_path_value = source.get("MONITOR_SPOOL_PATH")
    log_path_value = source.get("MONITOR_LOG_PATH")

    return AgentConfig(
        collector_uri=collector_uri,
        api_token=api_token,
        heartbeat_sec=_parse_int(source, "MONITOR_HEARTBEAT_SEC", 300, 30, 86400),
        startup_delay_sec=_parse_int(source, "MONITOR_STARTUP_DELAY_SEC", 30, 0, 3600),
        connect_timeout_sec=_parse_float(source, "MONITOR_CONNECT_TIMEOUT_SEC", 5.0, 0.1, 300.0),
        read_timeout_sec=_parse_float(source, "MONITOR_READ_TIMEOUT_SEC", 15.0, 0.1, 300.0),
        collection_timeout_sec=_parse_float(
            source, "MONITOR_COLLECTION_TIMEOUT_SEC", 30.0, 1.0, 3600.0
        ),
        max_collector_workers=_parse_int(source, "MONITOR_MAX_COLLECTOR_WORKERS", 4, 1, 32),
        spool_path=(Path(spool_path_value) if spool_path_value else _default_spool_path(platform)),
        spool_max_bytes=_parse_int(
            source,
            "MONITOR_SPOOL_MAX_BYTES",
            104857600,
            1048576,
            10737418240,
        ),
        spool_max_age_sec=_parse_int(source, "MONITOR_SPOOL_MAX_AGE_SEC", 604800, 3600, 31536000),
        replay_batch_size=_parse_int(source, "MONITOR_REPLAY_BATCH_SIZE", 20, 1, 1000),
        ca_bundle=ca_bundle,
        process_cmdline_mode=process_cmdline_mode,
        include_network_connections=_parse_bool(
            source, "MONITOR_INCLUDE_NETWORK_CONNECTIONS", True
        ),
        include_software=_parse_bool(source, "MONITOR_INCLUDE_SOFTWARE", True),
        log_path=Path(log_path_value) if log_path_value else _default_log_path(platform),
        log_format=log_format,
        log_level=source.get("MONITOR_LOG_LEVEL", "INFO").upper(),
    )
