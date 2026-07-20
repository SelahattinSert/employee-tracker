# Monitor Agent 2.0 Production Upgrade Design

**Status:** Approved for implementation planning  
**Date:** 2026-07-20  
**Release target:** `monitor-agent` 2.0.0  
**Production runtime:** CPython 3.14.6  
**Supported runtimes:** CPython 3.11 through 3.14  

## 1. Problem Statement

The current repository contains one 363-line monitoring script, a requirements file whose dependencies are entirely commented out, an invalid Windows Task Scheduler XML file, and a Linux service definition with hard-coded runtime paths and a literal token replacement marker. It has no package metadata, tests, automated quality gates, installation workflow, offline delivery, graceful shutdown, or configuration validation.

The upgrade must turn that script into a production endpoint telemetry agent without inventing a collector backend or breaking the existing `POST /api/v1/telemetry` contract.

## 2. Goals

1. Produce an installable, versioned Python package with a stable command-line interface.
2. Keep the existing telemetry schema compatible while adding operational metadata.
3. Isolate collector failures so one inaccessible host surface cannot suppress a heartbeat.
4. Survive collector downtime and process restarts without unbounded disk growth.
5. Protect credentials, machine identifiers, and sensitive process arguments.
6. Support managed deployment on Linux, Windows, and macOS.
7. Enforce formatting, typing, tests, packaging checks, and dependency auditing in CI.
8. Preserve the existing environment-variable interface where it remains safe.

## 3. Non-Goals

- Building or changing the remote telemetry collector.
- Adding a web dashboard, employee scoring, screenshots, keystroke capture, or content inspection.
- Running the endpoint agent in a container. Container isolation would hide the host surfaces the agent must observe.
- Adding a plugin framework. The fixed collector registry is smaller, safer, and sufficient for this release.
- Guaranteeing exactly-once delivery. The agent provides at-least-once delivery with an `event_id`; backend deduplication remains a collector responsibility.

## 4. Runtime and Dependency Strategy

Production deployments use CPython 3.14.6. The package supports CPython 3.11 through 3.14 so existing managed endpoints can migrate without a synchronized fleet-wide interpreter replacement.

Runtime dependencies are deliberately limited:

- `psutil==7.2.2` for cross-platform host telemetry.
- `requests==2.34.2` for mature TLS, connection pooling, and HTTP behavior.

The `schedule` dependency is removed. The runtime needs one non-overlapping periodic job, so a monotonic `threading.Event` loop provides scheduling and graceful shutdown without another package.

`pyproject.toml` becomes the source of package metadata, supported Python versions, CLI entry points, and tool configuration. A generated lock file pins the complete dependency graph used by deployments. Developer dependencies include `pytest`, `pytest-cov`, `ruff`, `mypy`, `build`, and `pip-audit`.

## 5. Package Architecture

```text
src/monitor_agent/
├── __init__.py
├── __main__.py
├── cli.py
├── config.py
├── identity.py
├── logging_setup.py
├── models.py
├── orchestrator.py
├── payload.py
├── runtime.py
├── spool.py
├── transport.py
└── collectors/
    ├── __init__.py
    ├── base.py
    ├── network.py
    ├── processes.py
    ├── resources.py
    ├── software.py
    ├── system.py
    └── users.py
```

Responsibilities are separated as follows:

- `config.py`: immutable configuration dataclass, environment parsing, range checks, URI validation, and secret-safe error messages.
- `identity.py`: stable, privacy-preserving machine identity with OS-specific sources and a persisted random fallback.
- `models.py`: typed collector results, telemetry envelope metadata, transport outcomes, and spool records.
- `collectors/`: host-surface collectors with no transport or scheduling knowledge.
- `orchestrator.py`: bounded concurrent execution, deadlines, result normalization, and aggregate collection status.
- `payload.py`: schema-compatible telemetry assembly.
- `transport.py`: HTTP session reuse, retry classification, backoff, and response handling.
- `spool.py`: atomic disk persistence, bounded retention, dead-letter handling, and ordered replay.
- `runtime.py`: startup event, heartbeat cadence, signal handling, replay coordination, and shutdown.
- `cli.py`: operational commands and stable exit codes.

The existing `agent/monitor_agent.py` remains as a one-release compatibility shim that invokes the packaged CLI. Deployment files use the new entry point directly.

## 6. Configuration Contract

The agent reads configuration from environment variables. Installers place production values in OS-protected environment files; secrets never appear directly in service definitions or task XML.

Existing names remain valid:

- `MONITOR_COLLECTOR_URI`
- `MONITOR_API_TOKEN`
- `MONITOR_HEARTBEAT_SEC`
- `MONITOR_STARTUP_DELAY_SEC`
- `MONITOR_LOG_PATH`

New settings provide bounded operational controls:

- `MONITOR_CONNECT_TIMEOUT_SEC`, default `5.0`
- `MONITOR_READ_TIMEOUT_SEC`, default `15.0`
- `MONITOR_COLLECTION_TIMEOUT_SEC`, default `30.0`
- `MONITOR_MAX_COLLECTOR_WORKERS`, default `4`
- `MONITOR_SPOOL_PATH`, platform-specific state-directory default
- `MONITOR_SPOOL_MAX_BYTES`, default `104857600`
- `MONITOR_SPOOL_MAX_AGE_SEC`, default `604800`
- `MONITOR_REPLAY_BATCH_SIZE`, default `20`
- `MONITOR_CA_BUNDLE`, optional custom CA path
- `MONITOR_PROCESS_CMDLINE_MODE`, one of `none`, `redacted`, or `full`; default `redacted`
- `MONITOR_INCLUDE_NETWORK_CONNECTIONS`, default `true`
- `MONITOR_INCLUDE_SOFTWARE`, default `true`
- `MONITOR_LOG_FORMAT`, one of `text` or `json`; default `text`
- `MONITOR_LOG_LEVEL`, default `INFO`

`MONITOR_COLLECTOR_URI` and `MONITOR_API_TOKEN` are required for `run` and transmitting `once` commands. The URI must use HTTPS. TLS verification cannot be disabled; private PKI is supported through `MONITOR_CA_BUNDLE`.

Numeric settings have explicit safe ranges. Configuration failure exits before collectors start and identifies the setting without printing its value when it may contain a secret.

## 7. Machine Identity

Raw operating-system identifiers never leave the endpoint. The source order is:

1. Linux `/etc/machine-id`.
2. Windows `HKLM\\SOFTWARE\\Microsoft\\Cryptography\\MachineGuid`.
3. macOS `IOPlatformUUID`.
4. A persisted random UUID stored with owner-only permissions in the agent state directory.

The selected source is combined with an application namespace and reduced with SHA-256 to a UUID-shaped identifier. This replaces the hostname-derived MD5 value, avoids leaking the raw platform identifier, and stays stable across ordinary reboots and hostname changes.

## 8. Collection Pipeline

The orchestrator submits independent collectors to a `ThreadPoolExecutor` capped by `MONITOR_MAX_COLLECTOR_WORKERS`. Every collector reports:

- `name`
- `status`: `success`, `partial`, `disabled`, `timed_out`, or `failed`
- `duration_ms`
- `data`
- a sanitized `error_code` and `error_message` when unsuccessful

The cycle deadline bounds total collection time. Timed-out or inaccessible surfaces become structured status entries; they do not terminate the heartbeat. Collectors remain best-effort across privilege differences, filesystems, package managers, and transient process exits.

Process command lines pass through a redactor that masks values following common secret-bearing flags and environment-style assignments. Operators can disable command-line collection entirely. Full unredacted collection requires an explicit setting.

The software collector caches results for 24 hours because installed-package enumeration is substantially more expensive than resource telemetry and rarely changes between five-minute heartbeats.

## 9. Payload Compatibility

The top-level `schema_version` remains `1.0`, and existing keys remain present:

- `event`
- `timestamp`
- `machine_id`
- `system`
- `users`
- `cpu`
- `memory`
- `disks`
- `network`
- `processes`
- `software`

The release adds only additive fields:

- `event_id`: UUID generated once and preserved through retries and spool replay.
- `agent`: version, Python version, platform, total collection duration, and collector status map.

Existing headers remain, with `Idempotency-Key: <event_id>` and `User-Agent: monitor-agent/2.0.0` added. The agent uses `requests`' `json=` parameter instead of manual serialization.

## 10. Transport and Retry Semantics

One `requests.Session` is reused for connection pooling. Connect and read timeouts are distinct. The agent classifies outcomes instead of retrying every failure:

- Success: HTTP `200` through `299`.
- Retriable: connection errors, timeouts, HTTP `408`, `425`, `429`, and `500` through `599`.
- Authentication rejection: HTTP `401` and `403`.
- Permanent rejection: other HTTP `400` through `499` responses.

Retriable failures use exponential backoff with full jitter and honor a bounded `Retry-After` value. A failed live event is spooled after in-memory retries are exhausted. Permanent rejections move an already-spooled record to a bounded dead-letter area and do not block later records.

Authentication failures emit a distinct operational error without logging the token. A rejected live event enters the bounded spool, a rejected replay record remains queued, and further replay pauses for that cycle so a corrected token can recover the queue without turning valid telemetry into dead letters.

## 11. Offline Spool

Each spool record is a UTF-8 JSON file named by timestamp and event ID. Writes use a same-directory temporary file, file `fsync`, `os.replace`, and a directory `fsync` on POSIX so a crash cannot expose a partial record. File permissions are owner-only where the platform supports POSIX modes.

Replay is oldest-first and bounded by `MONITOR_REPLAY_BATCH_SIZE` per heartbeat. Replay happens before the new live payload. If records remain after the replay batch, the new payload is spooled behind them instead of being transmitted out of order. Successfully transmitted records are deleted. Corrupt files move to dead-letter storage with a sanitized log entry.

Retention enforces both age and total bytes. Oldest records are evicted first when either limit is exceeded, and every eviction is logged with record counts rather than payload contents.

## 12. Runtime and CLI

The runtime uses `time.monotonic()` and `threading.Event.wait()` to prevent wall-clock adjustments from distorting the heartbeat cadence. Jobs never overlap. `SIGTERM`, `SIGINT`, Windows service termination, and normal process exit all set the same stop event.

Commands are:

- `monitor-agent run`: startup event, scheduled heartbeats, replay, and graceful shutdown.
- `monitor-agent once --event <name>`: one collection cycle, optionally printed without transmission.
- `monitor-agent check-config`: configuration and writable-path validation without collecting or transmitting telemetry.
- `monitor-agent health`: configuration, identity source, spool state, dependency versions, and collector availability without revealing sensitive data.
- `monitor-agent version`: package and runtime version output.

CLI exit codes distinguish success, configuration failure, collection failure, and transport failure for automation.

## 13. Logging

Logs contain UTC timestamps, level, component, event ID when available, and concise operational context. They never contain tokens, authorization headers, complete telemetry payloads, raw machine-identity sources, or unredacted process arguments.

Text logs are the operator default. JSON logs are available for aggregation. Linux defaults to stdout and journald. Windows and macOS installers configure rotating files with restrictive permissions. Rotation is size-bounded and retains a fixed number of files.

## 14. Deployment Design

### Linux

The installer creates an isolated virtual environment under `/opt/monitor-agent`, a protected configuration file under `/etc/monitor-agent`, and state under `/var/lib/monitor-agent`. The service runs as root to preserve system-wide process and connection visibility, but applies systemd sandboxing including `NoNewPrivileges`, `ProtectSystem=strict`, `ProtectHome=read-only`, `PrivateTmp`, `RestrictSUIDSGID`, `LockPersonality`, `MemoryDenyWriteExecute`, a restrictive `UMask`, and explicit writable state paths.

The unit uses `EnvironmentFile` rather than inline secrets and validates with `systemd-analyze verify` in CI. Install, upgrade, and uninstall scripts are idempotent.

### Windows

A PowerShell installer creates `C:\\ProgramData\\MonitorAgent`, its virtual environment, protected configuration, logs, and spool directories. It resolves the installed Python path instead of hard-coding `C:\\Python311`. The scheduled task runs as LocalSystem at boot, uses a valid XML declaration, restarts on failure, and records a bounded execution history. Directory ACLs restrict configuration and telemetry state to Administrators and SYSTEM.

### macOS

A signed-ready launchd layout uses `/Library/Application Support/MonitorAgent` for the environment and state, `/Library/Logs/MonitorAgent` for logs, and a `LaunchDaemon` plist with `KeepAlive` behavior. Installation and removal require administrator privileges and set restrictive ownership and modes.

## 15. Testing and Quality Gates

The test suite uses `pytest` and targets at least 90% line and branch coverage for the package. Tests cover:

- Valid, missing, malformed, out-of-range, and secret-bearing configuration.
- OS identity sources, hashing, permissions, and persisted fallback behavior.
- Every collector's normal, partial, access-denied, disappearing-process, and unavailable-command paths.
- Concurrent orchestration, timeouts, collector isolation, and deterministic ordering.
- Schema compatibility and additive metadata.
- HTTP success, retry classes, permanent rejection, backoff bounds, `Retry-After`, and secret-safe logging.
- Atomic spool writes, corrupt records, size and age eviction, replay order, and dead-letter behavior.
- Runtime cadence, startup behavior, clean shutdown, and non-overlapping jobs.
- CLI commands and exit codes.
- Linux unit validation, Windows XML parsing, macOS plist parsing, and installer dry-run behavior.

CI runs on Linux, Windows, and macOS across Python 3.11, 3.12, 3.13, and 3.14 where available. Required checks are Ruff formatting and linting, strict mypy, pytest with coverage, wheel and source-distribution build, package metadata validation, and `pip-audit`.

Network tests use local fakes. CI never transmits host telemetry to the configured production endpoint.

## 16. Operator Materials

The release includes:

- `README.md` with installation, configuration, CLI, and platform deployment paths.
- `CHANGELOG.md` beginning with the 2.0.0 release.
- `SECURITY.md` covering secret handling, TLS, identity, privileges, and vulnerability reporting.
- `PRIVACY.md` listing every collected field and the controls for sensitive surfaces.
- `docs/migration-v1-to-v2.md` with environment compatibility, path changes, rollout, and rollback.
- `docs/operations.md` with health checks, spool recovery, common failures, and log interpretation.

## 17. Migration and Rollback

The upgrade process stops the existing service or task, preserves its configuration, installs the 2.0.0 environment beside the old runtime, runs `check-config` and a non-transmitting `once` smoke test, switches the service definition, and starts the new agent. The old runtime remains available until the first successful heartbeat and spool replay check.

Rollback restores the prior service definition and executable path without deleting the v2 spool. Configuration migration is additive, so existing environment names continue to work after required secrets and HTTPS validation pass.

## 18. Acceptance Criteria

The upgrade is complete when all of the following are true:

1. A clean environment can build and install the package on Python 3.11 through 3.14.
2. `monitor-agent check-config` rejects missing credentials, non-HTTPS collector URIs, invalid ranges, and unwritable state paths before collection starts.
3. `monitor-agent once --no-transmit` produces a schema-compatible payload with collector statuses and no unredacted secrets.
4. One collector can fail or time out while the remaining telemetry still forms a valid payload.
5. Simulated collector downtime persists events atomically, respects retention limits, and replays them oldest-first after recovery.
6. The runtime handles termination without starting another collection cycle and exits within ten seconds when no collector is stuck beyond its configured deadline.
7. Linux, Windows, and macOS deployment definitions parse and pass their platform-specific validation checks.
8. The compatibility shim runs the packaged entry point for existing script-based deployments.
9. Ruff, strict mypy, package builds, dependency audit, and all tests pass in CI.
10. Test coverage is at least 90% for both lines and branches.
11. No token, authorization header, raw platform machine identifier, complete payload, or unredacted process argument appears in logs.
12. Migration and rollback procedures complete without deleting queued telemetry.
