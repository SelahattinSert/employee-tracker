# Monitor Agent

Monitor Agent 2.0 is a cross-platform endpoint telemetry agent that collects bounded operational facts, delivers them over verified HTTPS, and preserves undelivered records in a permission-protected local spool.

## What the agent collects

The agent collects system, user-session, resource, disk, network, process, and installed-software telemetry. It can also collect explicitly enabled active-window metadata, bounded file-integrity metadata, and screenshots. It adds collector status metadata so a denied or timed-out source does not hide the rest of an event.

Active-window, file-audit, and screenshot collectors are disabled until their specific controls are configured and `MONITOR_EMPLOYEE_NOTICE_ACK=true` confirms that the required employee notice has been completed. File audit reads bounded files locally to compute SHA-256 but transmits no file contents. Screenshot payloads can contain any visible screen content and receive no password-field or application-content redaction. The agent does not collect keystrokes, clipboard contents, browser history, cookies, DOM data, or employee scoring.

Raw platform machine identifiers are never transmitted; an available platform identifier is namespaced and reduced with SHA-256. If that source is unavailable, a persisted owner-only random UUID supplies the stable `machine_id`. Process command lines default to secret redaction.

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
| `MONITOR_INCLUDE_ACTIVE_WINDOW` | `false` | `true`/`false`; requires employee notice acknowledgement | Sensitive telemetry |
| `MONITOR_AUDIT_PATHS` | Empty | `:`-separated paths on Linux/macOS; `;`-separated paths on Windows | Sensitive telemetry |
| `MONITOR_AUDIT_MAX_FILES` | `50` | `1..1000` integer | Collection bound |
| `MONITOR_AUDIT_MAX_FILE_BYTES` | `10485760` | `1024..1073741824` integer | Collection bound |
| `MONITOR_SCREENSHOT_ENABLED` | `false` | `true`/`false`; requires employee notice acknowledgement | Highly sensitive telemetry |
| `MONITOR_SCREENSHOT_MAX_BYTES` | `5242880` | `1024..52428800` integer | Collection bound |
| `MONITOR_EMPLOYEE_NOTICE_ACK` | `false` | Must be `true` before any optional sensitive collector is enabled | Compliance control |
| `MONITOR_SESSION_SOURCE` | `direct` | `direct` captures in this process; `helper` asks the `session-helper` running in each desktop login (required for the system service) | Operational |
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
| `monitor-agent session-helper` | Run inside a desktop login to serve screenshot and active-window capture to the service. |

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

## Download a ready-made installer

Pushing a version tag (`vMAJOR.MINOR.PATCH`) runs the `release` workflow, which builds a
single-file installer per platform and attaches them to the matching GitHub Release:

| Platform | Asset | Double-click behaviour |
| --- | --- | --- |
| Windows | `MonitorAgent-Windows-<ver>-Setup.exe` | Self-extracting `.exe`; runs `Install.cmd` (asks for server address, token and the notice confirmation). |
| Linux | `MonitorAgent-Linux-<ver>.run` | Self-extracting script; run `sudo ./…​.run` or "Run as Program". |
| macOS | `MonitorAgent-macOS-<ver>.dmg` | Disk image; open it and double-click `Install.command`. |

The installers contain no server URL, token or configuration; each one runs the platform
setup wizard, which asks for those at install time. Download assets from the repository's
**Releases** page (not Packages). To cut a release, tag a commit and push the tag:

```bash
git tag v2.0.0 && git push origin v2.0.0
```

## Install the service

Build the wheel first and provide a protected environment file. Installers require administrator or root privileges because they create service identities, protected runtime paths, and owner-only telemetry storage.

Linux:

```bash
sudo deploy/linux/install.sh dist/monitor_agent-2.0.0-py3-none-any.whl /secure/path/monitor-agent.env
sudo systemctl status monitor-agent.service
sudo journalctl -u monitor-agent.service
```

Windows, one-click package:

```bash
python scripts/build_windows_package.py            # dist/MonitorAgent-Windows-2.0.0.zip
python scripts/build_windows_package.py --ca ca.crt  # also bundle a private CA certificate
```

CI also uploads the zip with the build artifacts. Copy it to the endpoint, extract it and
double-click `Install.cmd`. It asks for administrator rights, then for the server address
(`https://host:port`; `/api/v1/telemetry` is added when no path is given) and the agent API
token (hidden input). It then asks once whether employees have been notified (default yes);
answering yes writes `MONITOR_SCREENSHOT_ENABLED`, `MONITOR_INCLUDE_ACTIVE_WINDOW`,
`MONITOR_AUDIT_PATHS` (default `C:\Users`) and the `MONITOR_EMPLOYEE_NOTICE_ACK` compliance
flag as enabled, so a normal install collects screenshots, active-window titles and file
audit, while answering no leaves them off. The agent code keeps these collectors off by
default, so only this install-time choice turns them on; enable them only after employees
have been notified as local law requires. The package contains no URL, token or
configuration: `setup.ps1` writes the configuration to an owner-only temporary folder,
passes it to `install.ps1` and deletes it. A bundled `ca.crt` is installed to
`C:\ProgramData\MonitorAgentCA\ca.crt` and set as `MONITOR_CA_BUNDLE`. When Python
3.11-3.14 is missing, setup installs Python 3.14 for all users with `winget` if available.
For unattended installs run `deploy\windows\setup.ps1 -ServerUrl https://host:8443
-TokenFile C:\secure\token.txt -EmployeesNotified` (or `-NoEmployeesNotified`) from an
elevated PowerShell. `Uninstall.cmd` removes the tasks and runtimes.

Windows, manual install from elevated PowerShell:

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

Linux and macOS, one-click package:

```bash
python scripts/build_unix_package.py --os linux             # dist/MonitorAgent-Linux-2.0.0.tar.gz
python scripts/build_unix_package.py --os macos --ca ca.crt # bundle a private CA certificate too
```

CI also uploads these archives with the build artifacts. Copy the archive to the endpoint and
extract it. On Linux run `./Install.sh` (or "Run as Program"); on macOS double-click
`Install.command`. The wizard asks for administrator rights (re-running with `sudo`), then for
the server address (`https://host:port`; `/api/v1/telemetry` is added when no path is given) and
the agent API token (hidden input). It then asks once whether employees have been notified
(default yes); answering yes enables `MONITOR_SCREENSHOT_ENABLED`, `MONITOR_INCLUDE_ACTIVE_WINDOW`,
`MONITOR_AUDIT_PATHS` (default `/home` on Linux, `/Users` on macOS) and the
`MONITOR_EMPLOYEE_NOTICE_ACK` compliance flag, while answering no leaves them off. The agent code
keeps these collectors off by default, so only this install-time choice turns them on; enable
them only after employees have been notified as local law requires. The package ships no URL,
token or configuration: `setup.sh` writes the configuration to an owner-only temporary file,
passes it to the platform `install.sh` and deletes it. A bundled `ca.crt` is installed under a
protected directory and set as `MONITOR_CA_BUNDLE`. For unattended installs run
`sudo deploy/linux/setup.sh --server-url https://host:8443 --token-file /secure/token
--employees-notified` (or `--no-employees-notified`); `--audit-paths` and `--python` are also
accepted. The existing `uninstall.sh` scripts remove the service.

### Session helper

The system service cannot see a user's desktop, so screenshot and active-window collection comes from `monitor-agent session-helper`, which each installer starts in every graphical login. Set `MONITOR_SESSION_SOURCE=helper` in the service configuration (the examples do). The helper uses its own runtime that standard users can read and execute; it never reads the service configuration or API token. It authenticates the service with a random per-login key in an owner-only file and exchanges only bounded JSON.

| Platform | Helper runtime | Start mechanism |
| --- | --- | --- |
| Linux | `/opt/monitor-agent/venv` | `/etc/xdg/autostart/monitor-agent-session.desktop` |
| Windows | `C:\Program Files\MonitorAgentSession\venv` | `MonitorAgentSession` scheduled task at each logon (Users group, least privilege) |
| macOS | `/Library/Application Support/MonitorAgentSession/venv` | `/Library/LaunchAgents/com.company.monitor-agent.session.plist` |

The helper starts at each user's next login after installation. On macOS, grant Screen Recording and Automation permission to the helper's Python through an MDM privacy profile; without it captures return `partial` status. Linux Wayland sessions limit active-window titles to what the installed tools support.

Uninstalling without `--purge` or `-Purge` preserves protected configuration and spool state. Purge removes managed state and is destructive.

## Upgrade and rollback

Follow the [v1 to v2 migration procedure](docs/migration-v1-to-v2.md) for preflight backups, staging validation, the managed-service switch, and platform-specific rollback. Use the [operations guide](docs/operations.md) for health, queue, rotation, and recovery procedures.

## Telemetry schema compatibility

Telemetry uses `schema_version` `1.1`. The legacy top-level fields remain present: `schema_version`, `event`, `timestamp`, `machine_id`, `system`, `users`, `cpu`, `memory`, `disks`, `network`, `processes`, and `software`. Schema 1.1 adds `active_window`, `file_audit`, and `screenshot`; disabled collectors retain stable empty or null values. Version 2 also includes `event_id` and `agent` metadata.

## Security and privacy

Read [SECURITY.md](SECURITY.md) before deployment and [PRIVACY.md](PRIVACY.md) before enabling optional data sources or changing command-line collection mode.
