from __future__ import annotations

import re
from pathlib import Path


def _read(path: str) -> str:
    return Path(path).read_text(encoding="utf-8")


def _ordered(text: str, headings: tuple[str, ...]) -> None:
    positions = [text.index(heading) for heading in headings]
    assert positions == sorted(positions)


def _table_rows(text: str, heading: str) -> list[list[str]]:
    section = text[text.index(heading) :]
    rows: list[list[str]] = []
    table_started = False
    for line in section.splitlines()[1:]:
        if not line.startswith("|"):
            if table_started:
                break
            continue
        if set(line.replace("|", "")) <= {"-", " "}:
            continue
        table_started = True
        rows.append([cell.strip() for cell in line.strip().strip("|").split("|")])
    return rows[1:]


def test_readme_defines_the_operator_contract_in_order() -> None:
    text = _read("README.md")
    _ordered(
        text,
        (
            "## What the agent collects",
            "## Supported platforms",
            "## Build from source",
            "## Required configuration",
            "## CLI commands",
            "## Install the service",
            "## Upgrade and rollback",
            "## Telemetry schema compatibility",
            "## Security and privacy",
        ),
    )
    for value in (
        "Python 3.11",
        "Python 3.14",
        "Linux",
        "Windows",
        "macOS",
        "monitor-agent run",
        "monitor-agent once",
        "monitor-agent check-config",
        "monitor-agent health",
        "monitor-agent version",
        "deploy/linux/install.sh",
        "deploy/windows/install.ps1",
        "deploy/macos/install.sh",
        "docs/migration-v1-to-v2.md",
        "docs/operations.md",
        "schema_version",
        "1.0",
        "SECURITY.md",
        "PRIVACY.md",
    ):
        assert value in text
    assert "screenshots" in text
    assert "keystrokes" in text
    assert "file contents" in text
    assert "browser content" in text
    assert "employee scoring" in text
    assert "persisted owner-only random UUID" in text
    assert "permission-protected local spool" in text
    assert "controls all network adapter, connection, and I/O telemetry" in text
    assert "For non-root local verification, select an owner-writable `MONITOR_SPOOL_PATH`" in text
    assert (
        "python -m venv .venv\n"
        ". .venv/bin/activate\n"
        'python -m pip install -e ".[dev]"\n'
        "monitor-agent version\n"
        "monitor-agent once --event heartbeat --no-transmit\n"
        "python -m pytest"
    ) in text
    for variable in (
        "MONITOR_COLLECTOR_URI",
        "MONITOR_API_TOKEN",
        "MONITOR_CA_BUNDLE",
        "MONITOR_HEARTBEAT_SEC",
        "MONITOR_STARTUP_DELAY_SEC",
        "MONITOR_CONNECT_TIMEOUT_SEC",
        "MONITOR_READ_TIMEOUT_SEC",
        "MONITOR_COLLECTION_TIMEOUT_SEC",
        "MONITOR_MAX_COLLECTOR_WORKERS",
        "MONITOR_SPOOL_PATH",
        "MONITOR_SPOOL_MAX_BYTES",
        "MONITOR_SPOOL_MAX_AGE_SEC",
        "MONITOR_REPLAY_BATCH_SIZE",
        "MONITOR_PROCESS_CMDLINE_MODE",
        "MONITOR_INCLUDE_NETWORK_CONNECTIONS",
        "MONITOR_INCLUDE_SOFTWARE",
        "MONITOR_LOG_PATH",
        "MONITOR_LOG_FORMAT",
        "MONITOR_LOG_LEVEL",
    ):
        assert variable in text
    for value in ("30..86400", "1048576..10737418240", "none`, `redacted`, `full"):
        assert value in text
    assert "`0` | Success" in text
    assert "`2` | Command-line, configuration, path, or invalid-event failure" in text
    assert "`3` | All collectors fail or time out during `once --no-transmit`" in text
    assert "`4` | A transmitting `once` event is not delivered" in text

    expected_configuration = {
        "`MONITOR_COLLECTOR_URI`": ("Required for transport", "Valid HTTPS URI", "Operational"),
        "`MONITOR_API_TOKEN`": ("Required for transport", "Non-empty string", "Secret"),
        "`MONITOR_CA_BUNDLE`": ("System trust store", "Existing regular-file", "Operational"),
        "`MONITOR_HEARTBEAT_SEC`": ("`300`", "`30..86400`", "No"),
        "`MONITOR_STARTUP_DELAY_SEC`": ("`30`", "`0..3600`", "No"),
        "`MONITOR_CONNECT_TIMEOUT_SEC`": ("`5.0`", "`0.1..300.0`", "No"),
        "`MONITOR_READ_TIMEOUT_SEC`": ("`15.0`", "`0.1..300.0`", "No"),
        "`MONITOR_COLLECTION_TIMEOUT_SEC`": ("`30.0`", "`1.0..3600.0`", "No"),
        "`MONITOR_MAX_COLLECTOR_WORKERS`": ("`4`", "`1..32`", "No"),
        "`MONITOR_SPOOL_PATH`": ("Platform-specific", "Writable directory", "Sensitive telemetry"),
        "`MONITOR_SPOOL_MAX_BYTES`": ("`104857600`", "`1048576..10737418240`", "No"),
        "`MONITOR_SPOOL_MAX_AGE_SEC`": ("`604800`", "`3600..31536000`", "No"),
        "`MONITOR_REPLAY_BATCH_SIZE`": ("`20`", "`1..1000`", "No"),
        "`MONITOR_PROCESS_CMDLINE_MODE`": (
            "`redacted`",
            "`none`, `redacted`, `full`",
            "Privacy control",
        ),
        "`MONITOR_INCLUDE_NETWORK_CONNECTIONS`": ("`true`", "`true`/`false`", "Privacy control"),
        "`MONITOR_INCLUDE_SOFTWARE`": ("`true`", "`true`/`false`", "Privacy control"),
        "`MONITOR_LOG_PATH`": ("Platform-specific", "Writable file path", "Operational"),
        "`MONITOR_LOG_FORMAT`": ("`text`", "`text`, `json`", "No"),
        "`MONITOR_LOG_LEVEL`": ("`INFO`", "Standard Python logging level", "No"),
    }
    rows = _table_rows(text, "## Required configuration")
    by_variable = {row[0]: row for row in rows}
    assert set(by_variable) == set(expected_configuration)
    for variable, expected_cells in expected_configuration.items():
        row = by_variable[variable]
        assert len(row) == 4
        assert all(row)
        for cell, expected in zip(row[1:], expected_cells, strict=True):
            assert expected in cell


def test_release_security_and_privacy_materials_cover_required_controls() -> None:
    changelog = _read("CHANGELOG.md")
    assert changelog.startswith("# Changelog\n\n## 2.0.0 - 2026-07-20\n")
    for line in (
        "- Installable cross-platform package and operational CLI.",
        "- Failure-isolated collectors with structured status metadata.",
        "- Classified HTTP retries and bounded atomic offline spool.",
        "- Hardened Linux, Windows, and macOS deployment workflows.",
        "- Cross-platform tests, typing, linting, package, and dependency gates.",
        "- Production runtime target moves to Python 3.14.6.",
        "- Machine identity now derives from hashed platform identifiers.",
        "- Process command lines default to secret redaction.",
        "- Telemetry scheduling uses a monotonic standard-library loop.",
        "- In-process `schedule` dependency.",
        "- Inline service credentials and hard-coded Python 3.11 paths.",
    ):
        assert line in changelog

    security = _read("SECURITY.md")
    for value in (
        "HTTPS",
        "MONITOR_CA_BUNDLE",
        "owner-only",
        "root",
        "SYSTEM",
        "secret-safe",
        "pip-audit -r requirements.lock --disable-pip",
        "repository Security tab's private vulnerability report",
        "never open a public issue",
    ):
        assert value in security

    privacy = _read("PRIVACY.md")
    for section in (
        "system",
        "users",
        "cpu",
        "memory",
        "disks",
        "network",
        "processes",
        "software",
        "agent",
    ):
        assert f"`{section}`" in privacy
    for value in (
        "MONITOR_PROCESS_CMDLINE_MODE",
        "`none`",
        "`redacted`",
        "`full`",
        "can transmit secrets supplied by other processes",
        "screenshots",
        "keystrokes",
        "file contents",
        "browser content",
        "employee scoring",
        "SHA-256",
        "persisted random UUID",
        "never transmitted",
        "all network adapter, connection, and I/O telemetry",
        "per-record `source`",
        (
            "stable private identifier derived from an available platform identifier "
            "or a persisted fallback UUID"
        ),
    ):
        assert value in privacy
    expected_payload_sections = {
        "`schema_version`, `event`, `timestamp`, `event_id`",
        "`machine_id`",
        "`system`",
        "`users`",
        "`cpu`",
        "`memory`",
        "`disks`",
        "`network`",
        "`processes`",
        "`software`",
        "`agent`",
    }
    rows = _table_rows(privacy, "## Payload inventory")
    by_section = {row[0]: row for row in rows}
    assert set(by_section) == expected_payload_sections
    for row in rows:
        assert len(row) == 5
        assert all(row)
    assert "hashed machine ID" not in privacy


def test_migration_preserves_recovery_material_and_v2_spool() -> None:
    text = _read("docs/migration-v1-to-v2.md")
    for value in (
        "external, owner-restricted backup",
        "service/task/LaunchDaemon definition",
        "environment file",
        "enabled/running state",
        "sha256sum",
        "staging virtual environment",
        "monitor-agent check-config",
        "monitor-agent once --event heartbeat --no-transmit",
        '$staging = "C:\\SecureStaging\\monitor-agent-v2"',
        'py -3.14 -m venv "$staging\\venv"',
        'Scripts\\monitor-agent.exe" check-config',
        "first heartbeat",
        "Linux rollback",
        "Windows rollback",
        "macOS rollback",
        "systemctl daemon-reload",
        "Register-ScheduledTask",
        "launchctl bootstrap",
        "does not delete the v2 spool",
        "Do not use purge",
        "/root/monitor-agent-v1-backup/opt/monitor-agent",
        "/root/monitor-agent-v1-backup/etc/monitor-agent",
        "/root/monitor-agent-v1-backup/etc/systemd/system/monitor-agent.service",
        "Export-ScheduledTask",
        'icacls "$backup" /inheritance:r /grant:r "*S-1-5-18:(OI)(CI)F" "*S-1-5-32-544:(OI)(CI)F"',
        "if ($LASTEXITCODE -ne 0)",
        "C:\\ProgramData\\MonitorAgent\\venv",
        "C:\\ProgramData\\MonitorAgent\\run-agent.ps1",
        "C:\\ProgramData\\MonitorAgent\\monitor-agent.env",
        '"/Library/Application Support/MonitorAgent/venv"',
        '"/Library/Application Support/MonitorAgent/run-agent.sh"',
        '"/Library/Application Support/MonitorAgent/monitor-agent.env"',
        "explicitly excludes the v2 spool and logs",
        "/var/lib/monitor-agent-v2-staging",
        "C:\\SecureStaging\\monitor-agent-v2",
        "/var/root/MonitorAgentV2Staging",
        "approved secret manager has injected",
        "MONITOR_SPOOL_PATH",
        "abort if it already exists",
        "service-state.env",
        "enabled=true",
        "active=true",
        "state_code=",
        "running=",
        "launchctl print-disabled system",
        "loaded=",
        "disabled=",
        'case "$prior_enabled" in',
        'case "$prior_active" in',
        "Enable-ScheduledTask -TaskName MonitorAgent",
        "Disable-ScheduledTask -TaskName MonitorAgent",
        'if ($priorState.running -eq "true")',
        'case "$prior_disabled" in',
        "launchctl disable system/com.company.monitor-agent",
        "launchctl enable system/com.company.monitor-agent",
        "launchctl kickstart -k system/com.company.monitor-agent",
    ):
        assert value in text
    assert not re.search(
        r"(?im)^.*(?:rm|Remove-Item).*(?:MonitorAgent/spool|MonitorAgent\\spool).*$", text
    )
    assert 'rm -rf "/Library/Application Support/MonitorAgent"' not in text
    assert "Remove-Item -Recurse -Force C:\\ProgramData\\MonitorAgent" not in text


def test_operations_matches_delivery_and_recovery_behavior() -> None:
    text = _read("docs/operations.md")
    normalized = text.casefold()
    for value in (
        "monitor-agent health",
        "identity source",
        "pending count",
        "dead-letter count",
        "`200..299`",
        "`401`, `403`",
        "`408`, `425`, `429`, `500..599`",
        "other `4xx`",
        "malformed",
        "authentication failures keep live and replay records queued",
        "oldest-first",
        "MONITOR_REPLAY_BATCH_SIZE",
        "older than `MONITOR_SPOOL_MAX_AGE_SEC`",
        "MONITOR_SPOOL_MAX_BYTES",
        "filenames, counts, sizes, ownership, and hashes",
        "Never print record bodies",
        "CA rotation",
        "API-token rotation",
        "10 MiB",
        "five backups",
        "access-denied collectors",
        "package-manager timeout",
        "invalid configuration",
        "full spool",
        "clock changes",
        "time.monotonic()",
        "threading.Event.wait()",
        "journald",
        "C:\\ProgramData\\MonitorAgent\\logs\\monitor-agent.log",
        "/Library/Logs/MonitorAgent/monitor-agent.log",
        "/Library/Logs/MonitorAgent/launchd.stdout.log",
        "/Library/Logs/MonitorAgent/launchd.stderr.log",
        "delivery event_id=... kind=... status=...",
        "delivery kind=corrupt status=dead_letter",
        "success is quiet",
        "sha256sum",
        "shasum -a 256",
        "Get-FileHash",
        "Never use `cat`, `type`, `Get-Content`",
        "-printf '%f %s %u %g\\n' | wc -l",
        "-exec stat -f '%N %z %Su %Sg' {} \\; | wc -l",
        "$records.Count",
    ):
        assert value.casefold() in normalized
