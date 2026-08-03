# Monitor Agent Operations

Use this guide to verify service health, recover delivery, rotate trust material, and troubleshoot a managed Monitor Agent 2.0 deployment.

## Health and normal runtime

Run `monitor-agent health` through the protected service launcher or environment. It prints package and Python versions, identity source, spool pending count and bytes, dead-letter count, and collector status only. It does not print telemetry payload bodies.

At normal startup, the runtime emits a `startup` telemetry event after the configured startup delay and then emits periodic `heartbeat` telemetry events. These are event types, not guaranteed success log lines. Cadence uses `time.monotonic()` and `threading.Event.wait()`, so wall-clock changes do not accelerate or stall the interval. Event timestamps remain UTC wall-clock values.

Delivery success is quiet. A failed delivery logs sanitized `delivery event_id=... kind=... status=...` metadata, and a corrupt spool record logs `delivery kind=corrupt status=dead_letter`. Those log lines intentionally exclude record bodies and credentials.

| Platform | Status and logs |
| --- | --- |
| Linux | `systemctl status monitor-agent.service` and `journalctl -u monitor-agent.service`; service output goes to journald. |
| Windows | `Get-ScheduledTask -TaskName MonitorAgent`, `Get-ScheduledTaskInfo -TaskName MonitorAgent`, and `C:\ProgramData\MonitorAgent\logs\monitor-agent.log`. |
| macOS | `launchctl print system/com.company.monitor-agent`, `/Library/Logs/MonitorAgent/monitor-agent.log`, `/Library/Logs/MonitorAgent/launchd.stdout.log`, and `/Library/Logs/MonitorAgent/launchd.stderr.log`. |

Windows and macOS file logs rotate at 10 MiB with five backups. Keep their log directories owner-only.

## HTTP delivery outcomes

The transport classifies every result before it changes queue state:

| DeliveryKind | Responses or failures | Operator effect |
| --- | --- | --- |
| `success` | `200..299` | Acknowledge the event or replay record. |
| `authentication` | `401`, `403` | authentication failures keep live and replay records queued; replay pauses for the cycle so token rotation can recover the queue. |
| `retriable` | `408`, `425`, `429`, `500..599`, connection errors, timeouts, and other request failures | Retry with exponential full jitter and bounded `Retry-After`; queue after the final retry. |
| `permanent` | other `4xx`, malformed status, missing/non-integer status, and every other response | Do not queue a live rejection. Move a permanently rejected replay record to dead-letter. |

Replay is oldest-first and is limited by `MONITOR_REPLAY_BATCH_SIZE`. Replay happens before a new live event. When backlog remains, the new event is queued to preserve order.

## Queue retention and dead-letter handling

The spool deletes pending records older than `MONITOR_SPOOL_MAX_AGE_SEC`, then removes oldest pending records until total pending bytes fit within `MONITOR_SPOOL_MAX_BYTES`. Review those settings before increasing data collection.

Inspect dead-letter storage using filenames, counts, sizes, ownership, and hashes. Never print record bodies or payload contents in a terminal, ticket, or diagnostic bundle. Never use `cat`, `type`, `Get-Content`, an editor, or a pager on these records.

On Linux:

```bash
dead_letter=/var/lib/monitor-agent/spool/dead-letter
sudo find "$dead_letter" -maxdepth 1 -type f -name '*.json' -printf '%f %s %u %g\n' | wc -l
sudo find "$dead_letter" -maxdepth 1 -type f -name '*.json' -printf '%f %s %u %g\n'
sudo find "$dead_letter" -maxdepth 1 -type f -name '*.json' -exec sha256sum -- {} +
```

On macOS:

```bash
dead_letter="/Library/Application Support/MonitorAgent/spool/dead-letter"
sudo find "$dead_letter" -maxdepth 1 -type f -name '*.json' -exec stat -f '%N %z %Su %Sg' {} \; | wc -l
sudo find "$dead_letter" -maxdepth 1 -type f -name '*.json' -exec stat -f '%N %z %Su %Sg' {} \;
sudo find "$dead_letter" -maxdepth 1 -type f -name '*.json' -exec shasum -a 256 {} +
```

On Windows, from elevated PowerShell:

```powershell
$deadLetter = "C:\ProgramData\MonitorAgent\spool\dead-letter"
$records = Get-ChildItem -LiteralPath $deadLetter -File -Filter *.json
$records.Count
$records | Select-Object Name, Length, CreationTimeUtc
$records | ForEach-Object { Get-Acl -LiteralPath $_.FullName | Select-Object Path, Owner }
$records | ForEach-Object { Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256 }
```

A permanent replay rejection is the normal route to dead-letter; investigate the collector contract and event schema before reprocessing records.

## Certificate, token, and agent rotation

### CA rotation

Install the new CA bundle as an owner-restricted regular file, update `MONITOR_CA_BUNDLE` in the protected environment file, validate with `check-config`, then restart one canary service. Confirm a successful delivery before rotating the remaining fleet. TLS verification remains mandatory throughout the rotation.

### API-token rotation

Update the token only through the protected environment file or secret-management injection. Validate file ownership and run `check-config`, then restart the service. A `401` or `403` keeps queued records intact, so rotate the token first and allow oldest-first replay to recover the backlog.

### Agent upgrade

Build and checksum the release wheel, perform the staging no-transmit checks, then follow the [migration procedure](migration-v1-to-v2.md). Keep the external preflight backup until the first heartbeat and delivery verification complete.

## Troubleshooting

### Access-denied collectors

An access-denied collector is failure-isolated or partial; the event can still contain results from other collectors. Inspect the collector status and data requirement. Do not broaden privileges unless the documented data requirement justifies it.

### Package-manager timeout

Check proxy, DNS, package index, and CA trust settings from the build host. Retry the verified dependency command after correcting the network condition; do not substitute unpinned packages or bypass the lock-file audit.

### Invalid configuration

Run `monitor-agent check-config` through the same protected environment used by the service. Correct the reported URI, CA file, writable spool/log path, range, or protected-file permission. Keep secrets out of diagnostic output.

### Full spool

Inspect pending count and bytes with `monitor-agent health`, then verify collector authentication and reachability. Retention removes expired records first and then oldest records when the byte limit is exceeded. Increase limits only after confirming available protected storage and retention requirements.

### Clock changes

Wall-clock adjustments change event timestamps but not scheduling cadence. The runtime uses `time.monotonic()` and `threading.Event.wait()` for the startup and heartbeat loop. Investigate time synchronization separately if collector-side timestamps are unexpected.
