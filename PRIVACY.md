# Privacy

Monitor Agent collects operational telemetry for endpoint health and delivery diagnostics. It keeps the established `schema_version` `1.0` layout and adds only `event_id` plus `agent` metadata.

## Payload inventory

| Payload section | Fields | Purpose | Default state | Control |
| --- | --- | --- | --- | --- |
| `schema_version`, `event`, `timestamp`, `event_id` | Schema version, event name, UTC timestamp, UUID | Identify and order an event | Always included | Event name is restricted by the CLI |
| `machine_id` | Stable private identifier derived from an available platform identifier or a persisted fallback UUID | Correlate an endpoint without exposing a raw platform identifier | Always included | No raw `/etc/machine-id`, Windows MachineGuid, or macOS IOPlatformUUID is transmitted; the fallback is stored owner-only |
| `system` | Hostname, stable private identifier derived from an available platform identifier or a persisted fallback UUID, OS/release/version, architecture, processor, Python version, boot time, uptime | Describe the host runtime | Always included | Fixed collector |
| `users` | Name, terminal, remote host, start time, PID | Describe active sessions | Always included when available | Fixed collector |
| `cpu` | Physical/logical cores, total/per-core usage, frequency, load average | Diagnose resource pressure | Always included when available | Fixed collector |
| `memory` | RAM/swap totals, available/used values, percentages | Diagnose memory pressure | Always included when available | Fixed collector |
| `disks` | Device, mount point, filesystem, total/used/free capacity, percentage | Diagnose storage pressure | Always included when available | Fixed collector |
| `network` | Active adapter/interface data, IPv4, MAC, speed, MTU, connection endpoints/status/PID/fd, aggregate I/O counters | Diagnose connectivity and interface health | Enabled | `MONITOR_INCLUDE_NETWORK_CONNECTIONS` controls all network adapter, connection, and I/O telemetry |
| `processes` | PID, name, user, status, CPU, RSS, executable, command line, start time; maximum 100 records | Diagnose process health | Enabled | `MONITOR_PROCESS_CMDLINE_MODE` controls command-line treatment |
| `software` | Platform package/application name and version, per-record `source` | Inventory installed software | Enabled | `MONITOR_INCLUDE_SOFTWARE`; collector status remains in `agent` |
| `agent` | Package/Python/platform/collection-duration/identity-source metadata; collector status, duration, sanitized error code and message | Explain collection quality and agent state | Always included | Sanitized collector metadata only |

## Command-line privacy modes

`MONITOR_PROCESS_CMDLINE_MODE` controls every process command-line field:

- `none` sends an empty command-line string.
- `redacted` replaces known secret flags and secret-like assignments with `***`.
- `full` transmits raw arguments and can transmit secrets supplied by other processes. Use `full` only after a documented privacy review.

## Explicit exclusions

Monitor Agent does not collect screenshots, keystrokes, file contents, browser content, or employee scoring. Raw `/etc/machine-id`, Windows MachineGuid, and macOS IOPlatformUUID values are never transmitted; those values are used only to derive a namespaced SHA-256 machine identifier. When no platform identifier is available, the agent generates and protects a persisted random UUID instead.

## Storage and handling

Undelivered events remain in the protected spool until acknowledged, expired, evicted by the configured size limit, or moved to dead-letter after a permanent replay rejection. Treat spool records, logs, and collector output as sensitive telemetry. Inspect dead-letter records by filename, count, size, ownership, and hash rather than printing payload contents.
