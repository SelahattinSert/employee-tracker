# Monitor Agent

Monitor Agent 2.0 is a cross-platform endpoint telemetry agent that collects bounded operational facts, delivers them over verified HTTPS, and preserves undelivered records in a permission-protected local spool.

## What the agent collects

The agent collects system, user-session, resource, disk, network, process, and installed-software telemetry. It adds collector status metadata so a denied or timed-out source does not hide the rest of an event.

It does not collect screenshots, keystrokes, file contents, browser content, or employee scoring. Raw platform machine identifiers are never transmitted; an available platform identifier is namespaced and reduced with SHA-256. If that source is unavailable, a persisted owner-only random UUID supplies the stable `machine_id`. Process command lines default to secret redaction.

## Supported platforms

Run Monitor Agent on CPython **Python 3.11** through **Python 3.14**. The production target is CPython 3.14.6.

| Platform | Supported deployment surface |
| --- | --- |
| Linux | systemd on supported Linux distributions |
| Windows | Task Scheduler on supported Windows Server or desktop releases |
| macOS | system LaunchDaemon on supported macOS releases |

## Build from source

Build from a clean checkout with a supported Python interpreter:

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install -e ".[dev]"
python -m ruff format --check .
python -m ruff check .
python -m mypy src/monitor_agent
python -m pytest
python -m build
```

The wheel is written to `dist/monitor_agent-2.0.0-py3-none-any.whl`. Build artifacts are release inputs; do not install an unverified wheel on a managed host.

## Required configuration

Store configuration in the owner-restricted environment file consumed by the platform installer. Inject the API token from the approved secret-management system; never place a token in shell history, a command line, source control, or a support ticket.

| Variable | Default or requirement | Accepted values | Sensitivity |
| --- | --- | --- | --- |
| `MONITOR_COLLECTOR_URI` | Required for transport | Valid HTTPS URI without embedded credentials | Operational |
| `MONITOR_API_TOKEN` | Required for transport | Non-empty string | Secret |
| `MONITOR_CA_BUNDLE` | System trust store | Existing regular-file CA bundle | Operational |
| `MONITOR_HEARTBEAT_SEC` | `300` | `30..86400` integer | No |
| `MONITOR_STARTUP_DELAY_SEC` | `30` | `0..3600` integer | No |
| `MONITOR_CONNECT_TIMEOUT_SEC` | `5.0` | `0.1..300.0` | No |
| `MONITOR_READ_TIMEOUT_SEC` | `15.0` | `0.1..300.0` | No |
| `MONITOR_COLLECTION_TIMEOUT_SEC` | `30.0` | `1.0..3600.0` | No |
| `MONITOR_MAX_COLLECTOR_WORKERS` | `4` | `1..32` integer | No |
| `MONITOR_SPOOL_PATH` | Platform-specific | Writable directory | Sensitive telemetry |
| `MONITOR_SPOOL_MAX_BYTES` | `104857600` | `1048576..10737418240` integer | No |
| `MONITOR_SPOOL_MAX_AGE_SEC` | `604800` | `3600..31536000` integer | No |
| `MONITOR_REPLAY_BATCH_SIZE` | `20` | `1..1000` integer | No |
| `MONITOR_PROCESS_CMDLINE_MODE` | `redacted` | `none`, `redacted`, `full` | Privacy control |
| `MONITOR_INCLUDE_NETWORK_CONNECTIONS` | `true` | `true`/`false`, `1`/`0`, `yes`/`no`; controls all network adapter, connection, and I/O telemetry | Privacy control |
| `MONITOR_INCLUDE_SOFTWARE` | `true` | `true`/`false`, `1`/`0`, `yes`/`no` | Privacy control |
| `MONITOR_LOG_PATH` | Platform-specific | Writable file path | Operational |
| `MONITOR_LOG_FORMAT` | `text` | `text`, `json` | No |
| `MONITOR_LOG_LEVEL` | `INFO` | Standard Python logging level | No |

Default spool locations are `/var/lib/monitor-agent/spool` on Linux, `C:\ProgramData\MonitorAgent\spool` on Windows, and `/Library/Application Support/MonitorAgent/spool` on macOS. Linux services log to journald by default; the Windows and macOS default log paths are `C:\ProgramData\MonitorAgent\logs\monitor-agent.log` and `/Library/Logs/MonitorAgent/monitor-agent.log`.

TLS verification is always enabled. For a private PKI, set `MONITOR_CA_BUNDLE` to the protected CA bundle file.

## CLI commands

| Command | Use |
| --- | --- |
| `monitor-agent run` | Run the startup event and periodic heartbeat loop. |
| `monitor-agent once` | Collect and deliver one event. |
| `monitor-agent once --event heartbeat --no-transmit` | Validate collection and payload construction without a network request. |
| `monitor-agent check-config` | Validate protected configuration and writable paths. |
| `monitor-agent health` | Print operational health only, without telemetry payload contents. |
| `monitor-agent version` | Print package and Python versions. |

| Exit code | Meaning |
| --- | --- |
| `0` | Success |
| `2` | Command-line, configuration, path, or invalid-event failure |
| `3` | All collectors fail or time out during `once --no-transmit` |
| `4` | A transmitting `once` event is not delivered |

Quick verification:

For non-root local verification, select an owner-writable `MONITOR_SPOOL_PATH` through your protected local environment before running the no-transmit command. Do not put a collector credential in shell history.

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install -e ".[dev]"
monitor-agent version
monitor-agent once --event heartbeat --no-transmit
python -m pytest
```

## Install the service

Build the wheel first and provide a protected environment file. Installers require administrator or root privileges because they create service identities, protected runtime paths, and owner-only telemetry storage.

Linux:

```bash
sudo deploy/linux/install.sh dist/monitor_agent-2.0.0-py3-none-any.whl /secure/path/monitor-agent.env
sudo systemctl status monitor-agent.service
sudo journalctl -u monitor-agent.service
```

Windows, from elevated PowerShell:

The `deploy/windows/install.ps1` installer is the Windows deployment source.

```powershell
.\deploy\windows\install.ps1 -WheelPath .\dist\monitor_agent-2.0.0-py3-none-any.whl -EnvironmentFile C:\secure\monitor-agent.env -PythonVersion 3.14
Get-ScheduledTask -TaskName MonitorAgent
Get-ScheduledTaskInfo -TaskName MonitorAgent
```

macOS:

```bash
sudo deploy/macos/install.sh dist/monitor_agent-2.0.0-py3-none-any.whl /secure/path/monitor-agent.env
sudo launchctl print system/com.company.monitor-agent
```

Uninstalling without `--purge` or `-Purge` preserves protected configuration and spool state. Purge removes managed state and is destructive.

## Upgrade and rollback

Follow the [v1 to v2 migration procedure](docs/migration-v1-to-v2.md) for preflight backups, staging validation, the managed-service switch, and platform-specific rollback. Use the [operations guide](docs/operations.md) for health, queue, rotation, and recovery procedures.

## Telemetry schema compatibility

Telemetry remains at `schema_version` `1.0`. The legacy top-level fields remain present: `schema_version`, `event`, `timestamp`, `machine_id`, `system`, `users`, `cpu`, `memory`, `disks`, `network`, `processes`, and `software`. Version 2 adds `event_id` and `agent` metadata without removing legacy fields.

## Security and privacy

Read [SECURITY.md](SECURITY.md) before deployment and [PRIVACY.md](PRIVACY.md) before enabling optional data sources or changing command-line collection mode.
