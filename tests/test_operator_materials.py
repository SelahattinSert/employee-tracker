from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    not Path("docs").is_dir(),
    reason="operator docs are maintained locally and excluded from the repository",
)


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


def _fenced_block(text: str, heading: str, language: str) -> str:
    start = text.index(heading)
    fence = f"```{language}"
    block_start = text.index(fence, start) + len(fence)
    block_end = text.index("```", block_start)
    return text[block_start:block_end]


def _ordered_commands(block: str, commands: tuple[str, ...]) -> None:
    positions = [block.index(command) for command in commands]
    assert positions == sorted(positions)


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
    expected_payload_rows = {
        "`schema_version`, `event`, `timestamp`, `event_id`": (
            "Schema version, event name, UTC timestamp, UUID",
            "Identify and order an event",
            "Always included",
            "Event name is restricted by the CLI",
        ),
        "`machine_id`": (
            "Stable private identifier derived from an available platform identifier "
            "or a persisted fallback UUID",
            "Correlate an endpoint without exposing a raw platform identifier",
            "Always included",
            "No raw `/etc/machine-id`, Windows MachineGuid, or macOS IOPlatformUUID "
            "is transmitted; the fallback is stored owner-only",
        ),
        "`system`": (
            "Hostname, stable private identifier derived from an available platform "
            "identifier or a persisted fallback UUID, OS/release/version, architecture, "
            "processor, Python version, boot time, uptime",
            "Describe the host runtime",
            "Always included",
            "Fixed collector",
        ),
        "`users`": (
            "Name, terminal, remote host, start time, PID",
            "Describe active sessions",
            "Always included when available",
            "Fixed collector",
        ),
        "`cpu`": (
            "Physical/logical cores, total/per-core usage, frequency, load average",
            "Diagnose resource pressure",
            "Always included when available",
            "Fixed collector",
        ),
        "`memory`": (
            "RAM/swap totals, available/used values, percentages",
            "Diagnose memory pressure",
            "Always included when available",
            "Fixed collector",
        ),
        "`disks`": (
            "Device, mount point, filesystem, total/used/free capacity, percentage",
            "Diagnose storage pressure",
            "Always included when available",
            "Fixed collector",
        ),
        "`network`": (
            "Active adapter/interface data, IPv4, MAC, speed, MTU, connection "
            "endpoints/status/PID/fd, aggregate I/O counters",
            "Diagnose connectivity and interface health",
            "Enabled",
            "`MONITOR_INCLUDE_NETWORK_CONNECTIONS` controls all network adapter, "
            "connection, and I/O telemetry",
        ),
        "`processes`": (
            "PID, name, user, status, CPU, RSS, executable, command line, start time; "
            "maximum 100 records",
            "Diagnose process health",
            "Enabled",
            "`MONITOR_PROCESS_CMDLINE_MODE` controls command-line treatment",
        ),
        "`software`": (
            "Platform package/application name and version, per-record `source`",
            "Inventory installed software",
            "Enabled",
            "`MONITOR_INCLUDE_SOFTWARE`; collector status remains in `agent`",
        ),
        "`agent`": (
            "Package/Python/platform/collection-duration/identity-source metadata; "
            "collector status, duration, sanitized error code and message",
            "Explain collection quality and agent state",
            "Always included",
            "Sanitized collector metadata only",
        ),
    }
    rows = _table_rows(privacy, "## Payload inventory")
    by_section = {row[0]: row for row in rows}
    assert set(by_section) == set(expected_payload_rows)
    for section, expected_cells in expected_payload_rows.items():
        row = by_section[section]
        assert len(row) == 5
        assert all(row)
        assert tuple(row[1:]) == expected_cells
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


def test_migration_rollback_validates_recovery_before_v2_mutation_and_restores_states() -> None:
    text = _read("docs/migration-v1-to-v2.md")
    linux = _fenced_block(text, "### Linux rollback", "bash")
    _ordered_commands(
        linux,
        (
            "service-state.env",
            "invalid saved service state; aborting",
            '"$backup/opt/monitor-agent"',
            '"$backup/etc/monitor-agent/monitor-agent.env"',
            '"$backup/etc/systemd/system/monitor-agent.service"',
            "if sudo test -e /root/monitor-agent-v2-displaced; then",
            "sudo systemctl stop monitor-agent.service",
        ),
    )
    assert "state_lines=$(sudo awk 'END { print NR }' \"$state_file\")" in linux
    assert 'case "$prior_enabled:$prior_active" in' in linux
    assert "true:true|true:false|false:true|false:false" in linux

    windows = _fenced_block(text, "### Windows rollback", "powershell")
    _ordered_commands(
        windows,
        (
            "$statePath =",
            "saved task state is invalid",
            "$requiredBackupPaths = @(",
            "foreach ($requiredPath in $requiredBackupPaths)",
            "$currentTask = Get-ScheduledTask",
            "if (Test-Path -LiteralPath $displaced)",
            "New-Item -ItemType Directory -Path $displaced",
            "Stop-ScheduledTask -TaskName MonitorAgent",
            "Unregister-ScheduledTask -TaskName MonitorAgent",
        ),
    )
    _ordered_commands(
        windows,
        (
            "Register-ScheduledTask -TaskName MonitorAgent",
            'if ($priorState.running -eq "true")',
            'if ($priorState.enabled -eq "false") { Enable-ScheduledTask -TaskName MonitorAgent }',
            "Start-ScheduledTask -TaskName MonitorAgent",
            'if ($priorState.enabled -eq "true")',
            "Disable-ScheduledTask -TaskName MonitorAgent",
        ),
    )
    assert "state_code -notmatch '^[0-9]+$'" in windows
    assert "[bool]::Parse($priorState.running)" in windows
    assert '"enabled,running,state_code"' in windows

    macos = _fenced_block(text, "### macOS rollback", "bash")
    _ordered_commands(
        macos,
        (
            "launchd-state.env",
            "invalid saved LaunchDaemon state; aborting",
            '"$backup/runtime/venv"',
            '"$backup/configuration/monitor-agent.env"',
            '"$backup/LaunchDaemons/com.company.monitor-agent.plist"',
            "if sudo test -e /var/root/MonitorAgentV2Displaced; then",
            "sudo launchctl bootout system/com.company.monitor-agent",
        ),
    )
    _ordered_commands(
        macos,
        (
            "true) sudo launchctl enable system/com.company.monitor-agent",
            "sudo launchctl bootstrap system "
            "/Library/LaunchDaemons/com.company.monitor-agent.plist",
            "sudo launchctl kickstart -k system/com.company.monitor-agent",
            "true) sudo launchctl disable system/com.company.monitor-agent",
        ),
    )
    assert 'case "$prior_loaded:$prior_running" in' in macos
    assert "true:true|true:false|false:false" in macos
    assert 'case "$prior_loaded" in' in macos
    assert 'case "$prior_disabled" in' in macos


def test_migration_rollback_preflight_is_fail_fast_and_never_uses_unsafe_stop() -> None:
    text = _read("docs/migration-v1-to-v2.md")
    linux = _fenced_block(text, "### Linux rollback", "bash")
    assert linux.lstrip().startswith("set -eu")
    _ordered_commands(
        linux,
        (
            'sudo test -d "$backup/opt/monitor-agent"',
            'sudo test -f "$backup/etc/monitor-agent/monitor-agent.env"',
            'sudo test -f "$backup/etc/systemd/system/monitor-agent.service"',
            "sudo test -d /opt/monitor-agent",
            "sudo test -f /etc/monitor-agent/monitor-agent.env",
            "sudo test -f /etc/systemd/system/monitor-agent.service",
            "sudo install -d -m 0700 /root/monitor-agent-v2-displaced",
            "sudo systemctl stop monitor-agent.service",
        ),
    )

    windows = _fenced_block(text, "### Windows rollback", "powershell")
    assert windows.lstrip().startswith('$ErrorActionPreference = "Stop"')
    for path, path_type in (
        ("$backup\\monitor_agent_task.xml", "Leaf"),
        ("$backup\\runtime\\venv", "Container"),
        ("$backup\\runtime\\run-agent.ps1", "Leaf"),
        ("$backup\\runtime\\monitor_agent_task.xml", "Leaf"),
        ("$backup\\configuration\\monitor-agent.env", "Leaf"),
        ("C:\\ProgramData\\MonitorAgent\\venv", "Container"),
        ("C:\\ProgramData\\MonitorAgent\\run-agent.ps1", "Leaf"),
        ("C:\\ProgramData\\MonitorAgent\\monitor_agent_task.xml", "Leaf"),
        ("C:\\ProgramData\\MonitorAgent\\monitor-agent.env", "Leaf"),
    ):
        assert f'Path = "{path}"; Type = "{path_type}"' in windows
    _ordered_commands(
        windows,
        (
            "foreach ($requiredPath in $requiredBackupPaths)",
            "foreach ($requiredPath in $requiredV2Paths)",
            "$currentTask = Get-ScheduledTask -TaskName MonitorAgent -ErrorAction Stop",
            "New-Item -ItemType Directory -Path $displaced",
            "Stop-ScheduledTask -TaskName MonitorAgent",
            "Unregister-ScheduledTask -TaskName MonitorAgent -Confirm:$false",
        ),
    )
    assert "Test-Path -LiteralPath $requiredPath.Path -PathType $requiredPath.Type" in windows

    macos = _fenced_block(text, "### macOS rollback", "bash")
    assert macos.lstrip().startswith("set -eu")
    _ordered_commands(
        macos,
        (
            'sudo test -d "$backup/runtime/venv"',
            'sudo test -f "$backup/runtime/run-agent.sh"',
            'sudo test -f "$backup/configuration/monitor-agent.env"',
            'sudo test -f "$backup/LaunchDaemons/com.company.monitor-agent.plist"',
            'sudo test -d "/Library/Application Support/MonitorAgent/venv"',
            'sudo test -f "/Library/Application Support/MonitorAgent/run-agent.sh"',
            'sudo test -f "/Library/Application Support/MonitorAgent/monitor-agent.env"',
            "sudo test -f /Library/LaunchDaemons/com.company.monitor-agent.plist",
            "sudo launchctl print system/com.company.monitor-agent",
            "sudo launchctl bootout system/com.company.monitor-agent",
        ),
    )
    assert "launchctl stop" not in macos
    macos_idle_branch = macos[macos.index("\n    true:false)\n") :]
    _ordered_commands(
        macos_idle_branch,
        (
            "true:false)",
            "sudo launchctl bootstrap system "
            "/Library/LaunchDaemons/com.company.monitor-agent.plist",
            'if [ "$prior_disabled" = true ]; then',
            "sudo launchctl disable system/com.company.monitor-agent",
            "wait_for_launchdaemon_stopped",
            'case "$prior_disabled" in',
        ),
    )
    assert "cannot safely restore loaded, inactive LaunchDaemon" in macos
    safety_preflight = macos[
        : macos.index("sudo install -d -m 0700 /var/root/MonitorAgentV2Displaced")
    ]
    assert 'case "$prior_loaded:$prior_running:$prior_disabled" in' in safety_preflight
    assert "true:false:*)" in safety_preflight


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
