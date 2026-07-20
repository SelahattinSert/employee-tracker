# Monitor Agent 2.0 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the legacy single-file employee monitoring script with a production-grade, schema-compatible `monitor-agent` 2.0.0 package that survives host-data failures, collector outages, and managed fleet deployment.

**Architecture:** A typed Python package separates configuration, identity, collectors, orchestration, payload assembly, transport, durable spooling, runtime scheduling, logging, and CLI concerns. Collectors execute concurrently behind explicit result contracts; the runtime sends schema-compatible envelopes through a classified retry layer and bounded atomic spool.

**Tech Stack:** CPython 3.11-3.14, psutil 7.2.2, Requests 2.34.2, pytest, pytest-cov, Ruff, strict mypy, setuptools, pip-tools, pip-audit, systemd, PowerShell Task Scheduler, launchd, GitHub Actions.

## Global Constraints

- Release version is exactly `2.0.0`; production runtime is CPython 3.14.6 and supported runtimes are CPython 3.11 through 3.14.
- Runtime dependencies are limited to `psutil==7.2.2` and `requests==2.34.2`; remove `schedule`.
- Preserve telemetry `schema_version` `1.0` and every existing top-level payload key.
- Add only `event_id` and `agent` metadata at the payload top level.
- Require HTTPS for transmission; TLS verification cannot be disabled.
- Never log API tokens, authorization headers, complete payloads, raw platform identifiers, or unredacted process arguments.
- Spool writes are atomic and owner-only; retention defaults are 100 MiB and seven days.
- Replay is oldest-first. If a replay batch leaves backlog, spool the new event behind it.
- Keep `agent/monitor_agent.py` as a one-release compatibility shim.
- Tests must reach at least 90% line and branch coverage.
- Do not add a collector backend, dashboard, plugin framework, container image, screenshots, keystroke capture, or employee scoring.

**Scope check:** These components are one delivery pipeline rather than independent products: configuration and identity feed collectors, collectors feed the envelope, and transport/spool/runtime deliver it through the same deployment unit. One plan preserves interface order and produces a single testable release.

---

## File Map

### Package and build files

- Create `pyproject.toml`: package metadata, dependencies, console entry point, and tool configuration.
- Create `requirements.lock`: hash-pinned runtime dependency graph generated from `pyproject.toml`.
- Create `src/monitor_agent/__init__.py`: package version.
- Create `src/monitor_agent/__main__.py`: `python -m monitor_agent` entry point.
- Create `src/monitor_agent/cli.py`: command parsing, dependency assembly, signal wiring, and exit codes.
- Create `src/monitor_agent/config.py`: immutable environment configuration and validation.
- Create `src/monitor_agent/identity.py`: stable OS identity and protected fallback.
- Create `src/monitor_agent/logging_setup.py`: text/JSON formatting and rotation.
- Create `src/monitor_agent/models.py`: shared enums and dataclasses.
- Create `src/monitor_agent/orchestrator.py`: bounded collector execution.
- Create `src/monitor_agent/payload.py`: schema-compatible envelope construction.
- Create `src/monitor_agent/runtime.py`: startup, heartbeat, replay, and shutdown control.
- Create `src/monitor_agent/spool.py`: atomic queue, retention, and dead letters.
- Create `src/monitor_agent/transport.py`: HTTP delivery and retry classification.
- Create `src/monitor_agent/collectors/*.py`: isolated host collectors.
- Modify `agent/monitor_agent.py`: replace legacy implementation with compatibility shim.
- Modify `agent/requirements.txt`: make legacy installation pull the package's exact runtime dependencies.

### Deployment and operator files

- Modify `deploy/linux/monitor-agent.service` and create Linux install/uninstall scripts.
- Modify `deploy/windows/monitor_agent_task.xml` and create Windows launcher/install/uninstall scripts.
- Create `deploy/macos/com.company.monitor-agent.plist` and macOS launcher/install/uninstall scripts.
- Create `README.md`, `CHANGELOG.md`, `SECURITY.md`, `PRIVACY.md`, `docs/migration-v1-to-v2.md`, and `docs/operations.md`.
- Create `.github/workflows/ci.yml` and `.gitignore`.

### Tests

- Create focused tests under `tests/` matching each package module.
- Create deployment validation tests under `tests/deploy/`.

---

### Task 1: Installable Package and Version Command

**Files:**
- Create: `pyproject.toml`
- Create: `src/monitor_agent/__init__.py`
- Create: `src/monitor_agent/__main__.py`
- Create: `src/monitor_agent/cli.py`
- Create: `tests/test_package.py`
- Create: `.gitignore`

**Interfaces:**
- Produces: `monitor_agent.__version__: str`
- Produces: `monitor_agent.cli.main(argv: Sequence[str] | None = None) -> int`
- Produces: console command `monitor-agent`

- [ ] **Step 1: Write the package test**

```python
from monitor_agent import __version__
from monitor_agent.cli import main


def test_version_constant() -> None:
    assert __version__ == "2.0.0"


def test_version_command(capsys) -> None:
    assert main(["version"]) == 0
    assert capsys.readouterr().out.strip() == "monitor-agent 2.0.0"
```

- [ ] **Step 2: Run the test and verify the package does not exist**

Run: `python -m pytest tests/test_package.py -q`

Expected: collection fails with `ModuleNotFoundError: No module named 'monitor_agent'`.

- [ ] **Step 3: Add package metadata and the minimal version CLI**

```toml
[build-system]
requires = ["setuptools>=80", "wheel"]
build-backend = "setuptools.build_meta"

[project]
name = "monitor-agent"
version = "2.0.0"
description = "Cross-platform endpoint telemetry agent"
requires-python = ">=3.11,<3.15"
dependencies = [
  "psutil==7.2.2",
  "requests==2.34.2",
]

[project.optional-dependencies]
dev = [
  "build>=1.3",
  "mypy>=1.16",
  "pip-audit>=2.9",
  "pip-tools>=7.5",
  "pytest>=8.4",
  "pytest-cov>=6.2",
  "ruff>=0.12",
  "types-requests>=2.32",
]

[project.scripts]
monitor-agent = "monitor_agent.cli:entrypoint"

[tool.setuptools.packages.find]
where = ["src"]

[tool.pytest.ini_options]
addopts = "--strict-config --strict-markers"
testpaths = ["tests"]

[tool.coverage.run]
branch = true
source = ["monitor_agent"]

[tool.coverage.report]
fail_under = 90
show_missing = true

[tool.ruff]
target-version = "py311"
line-length = 100

[tool.ruff.lint]
select = ["E", "F", "I", "B", "UP", "SIM", "RUF"]

[tool.mypy]
python_version = "3.11"
strict = true
packages = ["monitor_agent"]
```

```python
# src/monitor_agent/__init__.py
__version__ = "2.0.0"
```

```python
# src/monitor_agent/cli.py
from __future__ import annotations

import argparse
from collections.abc import Sequence

from monitor_agent import __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="monitor-agent")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("version", help="print package version")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "version":
        print(f"monitor-agent {__version__}")
        return 0
    raise AssertionError(f"unhandled command: {args.command}")


def entrypoint() -> None:
    raise SystemExit(main())
```

```python
# src/monitor_agent/__main__.py
from monitor_agent.cli import entrypoint

entrypoint()
```

```gitignore
.coverage
.mypy_cache/
.pytest_cache/
.ruff_cache/
.venv/
__pycache__/
build/
dist/
*.egg-info/
*.py[cod]
```

- [ ] **Step 4: Install development dependencies and run the test**

Run: `python -m pip install -e '.[dev]'`

Run: `python -m pytest tests/test_package.py -q`

Expected: `2 passed`.

- [ ] **Step 5: Verify both entry points**

Run: `monitor-agent version`

Expected: `monitor-agent 2.0.0`.

Run: `python -m monitor_agent version`

Expected: `monitor-agent 2.0.0`.

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml .gitignore src/monitor_agent tests/test_package.py
git commit -m "build: package monitor agent 2.0"
```

---

### Task 2: Strict Environment Configuration

**Files:**
- Create: `src/monitor_agent/config.py`
- Create: `tests/test_config.py`

**Interfaces:**
- Produces: `ProcessCmdlineMode = Literal["none", "redacted", "full"]`
- Produces: `AgentConfig` immutable dataclass
- Produces: `load_config(env: Mapping[str, str] | None = None, *, require_transport: bool = True, platform_name: str | None = None) -> AgentConfig`
- Produces: `ConfigError`

- [ ] **Step 1: Write failing configuration tests**

```python
from pathlib import Path

import pytest

from monitor_agent.config import ConfigError, load_config


BASE_ENV = {
    "MONITOR_COLLECTOR_URI": "https://collector.internal/api/v1/telemetry",
    "MONITOR_API_TOKEN": "secret-token",
}


def test_load_config_defaults() -> None:
    config = load_config(BASE_ENV, platform_name="linux")
    assert config.heartbeat_sec == 300
    assert config.connect_timeout_sec == 5.0
    assert config.read_timeout_sec == 15.0
    assert config.collection_timeout_sec == 30.0
    assert config.max_collector_workers == 4
    assert config.spool_path == Path("/var/lib/monitor-agent/spool")
    assert config.process_cmdline_mode == "redacted"


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


def test_no_transmit_mode_allows_missing_transport_values() -> None:
    config = load_config({}, require_transport=False, platform_name="win32")
    assert config.collector_uri is None
    assert config.api_token is None
    assert config.spool_path == Path(r"C:\ProgramData\MonitorAgent\spool")


def test_transport_mode_never_echoes_token() -> None:
    with pytest.raises(ConfigError) as error:
        load_config({"MONITOR_API_TOKEN": "do-not-print"}, platform_name="linux")
    assert "do-not-print" not in str(error.value)
```

- [ ] **Step 2: Run tests and verify configuration symbols are missing**

Run: `python -m pytest tests/test_config.py -q`

Expected: collection fails because `monitor_agent.config` does not exist.

- [ ] **Step 3: Implement immutable validated configuration**

Create `AgentConfig` as `@dataclass(frozen=True, slots=True)` with these exact fields:

```python
collector_uri: str | None
api_token: str | None
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
```

Implement parsing with named helpers:

```python
def _parse_int(env: Mapping[str, str], name: str, default: int, minimum: int, maximum: int) -> int
def _parse_float(env: Mapping[str, str], name: str, default: float, minimum: float, maximum: float) -> float
def _parse_bool(env: Mapping[str, str], name: str, default: bool) -> bool
def _default_spool_path(platform_name: str) -> Path
def _default_log_path(platform_name: str) -> Path | None
```

Use these exact ranges:

- Heartbeat: 30 through 86400 seconds.
- Startup delay: 0 through 3600 seconds.
- Connect/read timeout: 0.1 through 300.0 seconds.
- Collection timeout: 1.0 through 3600.0 seconds.
- Collector workers: 1 through 32.
- Spool bytes: 1048576 through 10737418240.
- Spool age: 3600 through 31536000 seconds.
- Replay batch: 1 through 1000.

Validate the collector URI using `urllib.parse.urlsplit`. Require scheme `https`, a non-empty hostname, and no embedded username or password. Treat `MONITOR_CA_BUNDLE` as an existing regular file when set. Parse booleans only from `true/false`, `1/0`, and `yes/no`, case-insensitively.

- [ ] **Step 4: Run configuration tests**

Run: `python -m pytest tests/test_config.py -q`

Expected: all tests pass.

- [ ] **Step 5: Run static checks on the module**

Run: `ruff check src/monitor_agent/config.py tests/test_config.py`

Run: `mypy src/monitor_agent/config.py`

Expected: both commands exit 0.

- [ ] **Step 6: Commit**

```bash
git add src/monitor_agent/config.py tests/test_config.py
git commit -m "feat: validate agent configuration"
```

---

### Task 3: Stable Privacy-Preserving Machine Identity

**Files:**
- Create: `src/monitor_agent/identity.py`
- Create: `tests/test_identity.py`

**Interfaces:**
- Produces: `MachineIdentity(value: str, source: str)`
- Produces: `resolve_machine_identity(state_dir: Path, *, platform_name: str | None = None) -> MachineIdentity`

- [ ] **Step 1: Write failing identity tests**

```python
import re
from pathlib import Path

from monitor_agent.identity import MachineIdentity, resolve_machine_identity


UUID_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)


def test_linux_machine_id_is_hashed(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        "monitor_agent.identity._read_linux_machine_id",
        lambda: "raw-platform-machine-id",
    )
    identity = resolve_machine_identity(tmp_path, platform_name="linux")
    assert identity == resolve_machine_identity(tmp_path, platform_name="linux")
    assert identity.source == "linux-machine-id"
    assert identity.value != "raw-platform-machine-id"
    assert UUID_PATTERN.fullmatch(identity.value)


def test_missing_platform_id_persists_random_fallback(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("monitor_agent.identity._read_linux_machine_id", lambda: None)
    first = resolve_machine_identity(tmp_path, platform_name="linux")
    second = resolve_machine_identity(tmp_path, platform_name="linux")
    assert first == second
    assert first.source == "persisted-random"
    assert (tmp_path / "machine-id").stat().st_mode & 0o777 == 0o600


def test_hash_never_contains_raw_identifier(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        "monitor_agent.identity._read_macos_machine_id",
        lambda: "IOPlatformUUID-secret",
    )
    identity = resolve_machine_identity(tmp_path, platform_name="darwin")
    assert "secret" not in identity.value
```

- [ ] **Step 2: Run tests and verify the identity module is missing**

Run: `python -m pytest tests/test_identity.py -q`

Expected: collection fails because `monitor_agent.identity` does not exist.

- [ ] **Step 3: Implement OS identity sources and protected fallback**

```python
@dataclass(frozen=True, slots=True)
class MachineIdentity:
    value: str
    source: str
```

Implement these exact helpers:

```python
def _read_linux_machine_id() -> str | None
def _read_windows_machine_id() -> str | None
def _read_macos_machine_id() -> str | None
def _load_or_create_fallback(state_dir: Path) -> str
def _hashed_uuid(raw_identifier: str) -> str
```

Rules:

- Linux reads and strips `/etc/machine-id`; reject an empty value.
- Windows reads `MachineGuid` from `HKLM\SOFTWARE\Microsoft\Cryptography`.
- macOS runs `ioreg -rd1 -c IOPlatformExpertDevice` with a five-second timeout and extracts `IOPlatformUUID`.
- Fallback creates `state_dir/machine-id` with `os.open(..., O_CREAT | O_EXCL, 0o600)`, writes a UUID4, flushes and fsyncs it, and handles a concurrent creator by reading the winner's file.
- `_hashed_uuid` computes SHA-256 over `b"monitor-agent/v2\\0" + raw_identifier.encode()`, sets RFC 4122 variant/version bits in the first 16 digest bytes, and returns canonical lowercase UUID text.
- Never log or return the raw source value.

- [ ] **Step 4: Run identity tests**

Run: `python -m pytest tests/test_identity.py -q`

Expected: all tests pass.

- [ ] **Step 5: Run static checks**

Run: `ruff check src/monitor_agent/identity.py tests/test_identity.py`

Run: `mypy src/monitor_agent/identity.py`

Expected: both commands exit 0.

- [ ] **Step 6: Commit**

```bash
git add src/monitor_agent/identity.py tests/test_identity.py
git commit -m "feat: derive stable private machine identity"
```

---

### Task 4: Collector Result Contract and Orchestration

**Files:**
- Create: `src/monitor_agent/models.py`
- Create: `src/monitor_agent/collectors/__init__.py`
- Create: `src/monitor_agent/collectors/base.py`
- Create: `src/monitor_agent/orchestrator.py`
- Create: `tests/test_orchestrator.py`

**Interfaces:**
- Produces: recursive `JSONValue` type alias
- Produces: `CollectorStatus` enum
- Produces: `CollectorPayload`, `CollectorResult`, and `CollectionBatch` dataclasses
- Produces: `Collector` protocol
- Produces: `collect_all(collectors: Sequence[Collector], *, max_workers: int, timeout_sec: float) -> CollectionBatch`

- [ ] **Step 1: Write failing orchestration tests**

```python
import time

from monitor_agent.collectors.base import Collector
from monitor_agent.models import CollectorPayload, CollectorStatus
from monitor_agent.orchestrator import collect_all


class StaticCollector:
    def __init__(self, name: str, payload: CollectorPayload) -> None:
        self.name = name
        self.payload = payload

    def collect(self) -> CollectorPayload:
        return self.payload


class BrokenCollector:
    name = "broken"

    def collect(self) -> CollectorPayload:
        raise PermissionError("private path /secret")


class SlowCollector:
    name = "slow"

    def collect(self) -> CollectorPayload:
        time.sleep(0.2)
        return CollectorPayload(data={"slow": True})


def test_collect_all_preserves_registry_order_and_isolates_failure() -> None:
    collectors: list[Collector] = [
        StaticCollector("first", CollectorPayload(data={"first": 1})),
        BrokenCollector(),
        StaticCollector("last", CollectorPayload(data={"last": 3})),
    ]
    batch = collect_all(collectors, max_workers=3, timeout_sec=1.0)
    assert [result.name for result in batch.results] == ["first", "broken", "last"]
    assert batch.results[0].status is CollectorStatus.SUCCESS
    assert batch.results[1].status is CollectorStatus.FAILED
    assert batch.results[1].error_code == "permission_error"
    assert "/secret" not in (batch.results[1].error_message or "")
    assert batch.results[2].data == {"last": 3}


def test_collect_all_marks_deadline_without_dropping_fast_result() -> None:
    batch = collect_all(
        [
            SlowCollector(),
            StaticCollector("fast", CollectorPayload(data={"fast": True})),
        ],
        max_workers=2,
        timeout_sec=0.05,
    )
    assert batch.results[0].status is CollectorStatus.TIMED_OUT
    assert batch.results[1].status is CollectorStatus.SUCCESS
    assert batch.duration_ms < 150
```

- [ ] **Step 2: Run tests and verify shared contracts are missing**

Run: `python -m pytest tests/test_orchestrator.py -q`

Expected: collection fails because `monitor_agent.models` does not exist.

- [ ] **Step 3: Define exact shared contracts**

```python
class CollectorStatus(StrEnum):
    SUCCESS = "success"
    PARTIAL = "partial"
    DISABLED = "disabled"
    TIMED_OUT = "timed_out"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class CollectorPayload:
    data: JSONValue
    status: CollectorStatus = CollectorStatus.SUCCESS
    error_code: str | None = None
    error_message: str | None = None


@dataclass(frozen=True, slots=True)
class CollectorResult:
    name: str
    status: CollectorStatus
    duration_ms: int
    data: JSONValue
    error_code: str | None = None
    error_message: str | None = None


@dataclass(frozen=True, slots=True)
class CollectionBatch:
    results: tuple[CollectorResult, ...]
    duration_ms: int
```

Define `Collector` in `collectors/base.py`:

```python
class Collector(Protocol):
    name: str

    def collect(self) -> CollectorPayload: ...
```

- [ ] **Step 4: Implement bounded orchestration**

Use `ThreadPoolExecutor` and `concurrent.futures.wait` with the cycle deadline. Record `time.monotonic_ns()` before and after each collector. Convert exceptions to these sanitized codes:

- `PermissionError` and `psutil.AccessDenied` -> `permission_error`
- `subprocess.TimeoutExpired` and `TimeoutError` -> `timeout`
- `OSError` -> `os_error`
- all other `Exception` values -> `collector_error`

Do not copy exception text into errors because paths and command arguments can be sensitive. Use fixed messages such as `collector access denied`. Mark unfinished futures `TIMED_OUT` with empty mapping data, cancel pending work, and call `executor.shutdown(wait=False, cancel_futures=True)`. Preserve the input registry order in `CollectionBatch.results`.

- [ ] **Step 5: Run orchestration tests**

Run: `python -m pytest tests/test_orchestrator.py -q`

Expected: all tests pass in under one second.

- [ ] **Step 6: Run static checks**

Run: `ruff check src/monitor_agent/models.py src/monitor_agent/collectors src/monitor_agent/orchestrator.py tests/test_orchestrator.py`

Run: `mypy src/monitor_agent/models.py src/monitor_agent/collectors src/monitor_agent/orchestrator.py`

Expected: both commands exit 0.

- [ ] **Step 7: Commit**

```bash
git add src/monitor_agent/models.py src/monitor_agent/collectors src/monitor_agent/orchestrator.py tests/test_orchestrator.py
git commit -m "feat: isolate concurrent collector execution"
```

---

### Task 5: System and Resource Collectors

**Files:**
- Create: `src/monitor_agent/collectors/system.py`
- Create: `src/monitor_agent/collectors/resources.py`
- Create: `tests/collectors/test_system.py`
- Create: `tests/collectors/test_resources.py`

**Interfaces:**
- Consumes: `MachineIdentity` and `CollectorPayload`
- Produces: `SystemCollector(machine_identity: MachineIdentity)`
- Produces: `ResourceCollector(cpu_sample_sec: float = 1.0)`

- [ ] **Step 1: Write deterministic collector tests**

```python
# tests/collectors/test_system.py
from monitor_agent.collectors.system import SystemCollector
from monitor_agent.identity import MachineIdentity


def test_system_collector_preserves_schema(monkeypatch) -> None:
    monkeypatch.setattr("monitor_agent.collectors.system.socket.getfqdn", lambda: "host.example")
    monkeypatch.setattr("monitor_agent.collectors.system.psutil.boot_time", lambda: 1_700_000_000.0)
    collector = SystemCollector(MachineIdentity("hashed-id", "linux-machine-id"))
    result = collector.collect()
    system = result.data["system"]
    assert system["hostname"] == "host.example"
    assert system["machine_id"] == "hashed-id"
    assert system["boot_time"].endswith("+00:00")
    assert isinstance(system["uptime_sec"], int)
```

```python
# tests/collectors/test_resources.py
from types import SimpleNamespace

from monitor_agent.collectors.resources import ResourceCollector


def test_resource_collector_returns_cpu_memory_and_disks(monkeypatch) -> None:
    monkeypatch.setattr("monitor_agent.collectors.resources.psutil.cpu_count", lambda logical=True: 8 if logical else 4)
    monkeypatch.setattr(
        "monitor_agent.collectors.resources.psutil.cpu_percent",
        lambda interval, percpu=False: [10.0, 20.0] if percpu else 15.0,
    )
    monkeypatch.setattr(
        "monitor_agent.collectors.resources.psutil.cpu_freq",
        lambda: SimpleNamespace(current=2400.0),
    )
    monkeypatch.setattr(
        "monitor_agent.collectors.resources.psutil.virtual_memory",
        lambda: SimpleNamespace(total=4096, available=2048, used=2048, percent=50.0),
    )
    monkeypatch.setattr(
        "monitor_agent.collectors.resources.psutil.swap_memory",
        lambda: SimpleNamespace(total=1024, used=256, percent=25.0),
    )
    monkeypatch.setattr(
        "monitor_agent.collectors.resources.psutil.disk_partitions",
        lambda all=False: [SimpleNamespace(device="/dev/sda", mountpoint="/", fstype="ext4")],
    )
    monkeypatch.setattr(
        "monitor_agent.collectors.resources.psutil.disk_usage",
        lambda path: SimpleNamespace(total=10 * 1024**3, used=4 * 1024**3, free=6 * 1024**3, percent=40.0),
    )
    monkeypatch.setattr("monitor_agent.collectors.resources.psutil.getloadavg", lambda: (1.0, 2.0, 3.0))

    data = ResourceCollector(cpu_sample_sec=0).collect().data
    assert set(data) == {"cpu", "memory", "disks"}
    assert data["cpu"]["physical_cores"] == 4
    assert data["memory"]["ram"]["total_kb"] == 4
    assert data["disks"][0]["total_gb"] == 10.0
```

- [ ] **Step 2: Run tests and verify collectors are missing**

Run: `python -m pytest tests/collectors/test_system.py tests/collectors/test_resources.py -q`

Expected: collection fails for both missing modules.

- [ ] **Step 3: Implement `SystemCollector`**

Return `CollectorPayload(data={"system": system})` using:

- `platform.uname()` for OS fields.
- `datetime.fromtimestamp(psutil.boot_time(), tz=timezone.utc).isoformat()` for boot time.
- `max(0, int(time.time() - psutil.boot_time()))` for uptime.
- `sys.version` for Python runtime.
- The supplied hashed `MachineIdentity.value`, never its raw source.

Keep the existing keys `hostname`, `machine_id`, `os`, `os_release`, `os_version`, `architecture`, `processor`, `python`, `boot_time`, and `uptime_sec`.

- [ ] **Step 4: Implement `ResourceCollector`**

Collect CPU, memory, swap, and disk sections with the existing field names. Catch `PermissionError` and `OSError` per disk and return `PARTIAL` with code `disk_access_partial` when one or more partitions are skipped. Call `psutil.cpu_percent(interval=cpu_sample_sec, percpu=True)` once and derive total CPU from the mean of the per-core values; do not perform two blocking samples. Use `hasattr(psutil, "getloadavg")` for load average.

- [ ] **Step 5: Run collector tests and static checks**

Run: `python -m pytest tests/collectors/test_system.py tests/collectors/test_resources.py -q`

Run: `ruff check src/monitor_agent/collectors/system.py src/monitor_agent/collectors/resources.py tests/collectors`

Run: `mypy src/monitor_agent/collectors/system.py src/monitor_agent/collectors/resources.py`

Expected: all commands exit 0.

- [ ] **Step 6: Commit**

```bash
git add src/monitor_agent/collectors/system.py src/monitor_agent/collectors/resources.py tests/collectors
git commit -m "feat: collect system resource telemetry"
```

---

### Task 6: User and Process Collectors With Redaction

**Files:**
- Create: `src/monitor_agent/collectors/users.py`
- Create: `src/monitor_agent/collectors/processes.py`
- Create: `tests/collectors/test_users.py`
- Create: `tests/collectors/test_processes.py`

**Interfaces:**
- Consumes: `ProcessCmdlineMode` and `CollectorPayload`
- Produces: `UsersCollector`
- Produces: `ProcessesCollector(cmdline_mode: ProcessCmdlineMode, limit: int = 100)`
- Produces: `redact_command_line(arguments: Sequence[str], mode: ProcessCmdlineMode, *, platform_name: str | None = None) -> str`

- [ ] **Step 1: Write command-line redaction tests**

```python
import pytest

from monitor_agent.collectors.processes import redact_command_line


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        (["worker", "--token", "abc123", "--port", "443"], "worker --token *** --port 443"),
        (["worker", "--password=hunter2"], "worker '--password=***'"),
        (["worker", "API_KEY=secret", "MODE=prod"], "worker 'API_KEY=***' MODE=prod"),
        (["worker", "--authorization", "Bearer secret"], "worker --authorization ***"),
    ],
)
def test_redacted_mode_masks_secret_values(arguments: list[str], expected: str) -> None:
    assert redact_command_line(arguments, "redacted", platform_name="linux") == expected


def test_none_mode_returns_empty_string() -> None:
    assert redact_command_line(["worker", "--token", "secret"], "none") == ""


def test_full_mode_preserves_arguments() -> None:
    assert redact_command_line(["worker", "--port", "443"], "full", platform_name="linux") == "worker --port 443"
```

- [ ] **Step 2: Write process and user collector tests**

```python
# tests/collectors/test_processes.py
from types import SimpleNamespace

import psutil

from monitor_agent.collectors.processes import ProcessesCollector
from monitor_agent.models import CollectorStatus


class FakeProcess:
    def __init__(self, info=None, error=None) -> None:
        self._info = info
        self._error = error

    @property
    def info(self):
        if self._error is not None:
            raise self._error
        return self._info


def process_info(pid: int, rss: int, cmdline: list[str]) -> dict[str, object]:
    return {
        "pid": pid,
        "name": f"process-{pid}",
        "username": "employee",
        "status": "running",
        "cpu_percent": 4.5,
        "memory_info": SimpleNamespace(rss=rss),
        "exe": f"/usr/bin/process-{pid}",
        "cmdline": cmdline,
        "create_time": 1_700_000_000.0,
    }


def test_processes_sort_limit_redact_and_report_partial(monkeypatch) -> None:
    processes = [
        FakeProcess(process_info(1, 1024, ["worker", "--token", "secret"])),
        FakeProcess(error=psutil.AccessDenied(pid=2)),
        FakeProcess(process_info(3, 4096, ["server", "--port", "443"])),
        FakeProcess(error=psutil.NoSuchProcess(pid=4)),
    ]
    monkeypatch.setattr(
        "monitor_agent.collectors.processes.psutil.process_iter",
        lambda attrs: processes,
    )
    result = ProcessesCollector("redacted", limit=1).collect()
    assert result.status is CollectorStatus.PARTIAL
    assert result.error_code == "process_access_partial"
    assert result.data["processes"][0]["pid"] == 3
    assert len(result.data["processes"]) == 1
    assert "secret" not in repr(result.data)


# tests/collectors/test_users.py
from monitor_agent.collectors.users import UsersCollector


def test_users_preserve_existing_schema(monkeypatch) -> None:
    session = SimpleNamespace(
        name="employee",
        terminal="pts/1",
        host="10.0.0.4",
        started=1_700_000_000.0,
        pid=42,
    )
    monkeypatch.setattr("monitor_agent.collectors.users.psutil.users", lambda: [session])
    users = UsersCollector().collect().data["users"]
    assert users == [
        {
            "name": "employee",
            "terminal": "pts/1",
            "host": "10.0.0.4",
            "started": "2023-11-14T22:13:20+00:00",
            "pid": 42,
        }
    ]


def test_user_access_failure_is_partial(monkeypatch) -> None:
    monkeypatch.setattr(
        "monitor_agent.collectors.users.psutil.users",
        lambda: (_ for _ in ()).throw(PermissionError()),
    )
    result = UsersCollector().collect()
    assert result.status is CollectorStatus.PARTIAL
    assert result.error_code == "user_access_partial"
    assert result.data == {"users": []}
```

- [ ] **Step 3: Run tests and verify the modules are missing**

Run: `python -m pytest tests/collectors/test_users.py tests/collectors/test_processes.py -q`

Expected: collection fails for both missing modules.

- [ ] **Step 4: Implement redaction and process collection**

Treat these flag names as secret-bearing, case-insensitively:

```python
SECRET_FLAGS = frozenset(
    {
        "--api-key",
        "--apikey",
        "--access-token",
        "--authorization",
        "--password",
        "--secret",
        "--token",
    }
)
SECRET_ASSIGNMENT_PARTS = ("API_KEY", "AUTH", "PASSWORD", "SECRET", "TOKEN")
```

Mask both `--flag value` and `--flag=value`. Mask `NAME=value` when the uppercase name contains a secret assignment part. Join arguments with `shlex.join` on POSIX and `subprocess.list2cmdline` on Windows. Never include a skipped process's exception text in output or logs.

Return exactly the existing process keys: `pid`, `name`, `user`, `status`, `cpu_pct`, `mem_rss_kb`, `exe`, `cmdline`, and `started`.

- [ ] **Step 5: Implement user collection**

Convert `psutil.users()` to existing schema records. Use UTC-aware timestamps. Return `CollectorPayload(data={"users": users})`. On a top-level `PermissionError` or `OSError` return `PARTIAL`, an empty list, code `user_access_partial`, and fixed message `user sessions unavailable`.

- [ ] **Step 6: Run tests and static checks**

Run: `python -m pytest tests/collectors/test_users.py tests/collectors/test_processes.py -q`

Run: `ruff check src/monitor_agent/collectors/users.py src/monitor_agent/collectors/processes.py tests/collectors`

Run: `mypy src/monitor_agent/collectors/users.py src/monitor_agent/collectors/processes.py`

Expected: all commands exit 0.

- [ ] **Step 7: Commit**

```bash
git add src/monitor_agent/collectors/users.py src/monitor_agent/collectors/processes.py tests/collectors
git commit -m "feat: redact process telemetry"
```

---

### Task 7: Network and Cached Software Collectors

**Files:**
- Create: `src/monitor_agent/collectors/network.py`
- Create: `src/monitor_agent/collectors/software.py`
- Create: `tests/collectors/test_network.py`
- Create: `tests/collectors/test_software.py`

**Interfaces:**
- Produces: `NetworkCollector(enabled: bool = True)`
- Produces: `SoftwareCollector(enabled: bool = True, cache_ttl_sec: float = 86400.0)`

- [ ] **Step 1: Write network collector tests**

```python
import socket
from types import SimpleNamespace

import psutil

from monitor_agent.collectors.network import NetworkCollector
from monitor_agent.models import CollectorStatus


def install_network_fakes(monkeypatch, connection_result) -> None:
    monkeypatch.setattr(
        "monitor_agent.collectors.network.psutil.net_if_addrs",
        lambda: {
            "eth0": [
                SimpleNamespace(family=socket.AF_INET, address="10.0.0.2"),
                SimpleNamespace(family=psutil.AF_LINK, address="00:11:22:33:44:55"),
            ],
            "down0": [SimpleNamespace(family=socket.AF_INET, address="192.0.2.1")],
        },
    )
    monkeypatch.setattr(
        "monitor_agent.collectors.network.psutil.net_if_stats",
        lambda: {
            "eth0": SimpleNamespace(isup=True, speed=1000, mtu=1500),
            "down0": SimpleNamespace(isup=False, speed=0, mtu=1500),
        },
    )
    monkeypatch.setattr(
        "monitor_agent.collectors.network.psutil.net_io_counters",
        lambda: SimpleNamespace(
            bytes_sent=1,
            bytes_recv=2,
            packets_sent=3,
            packets_recv=4,
            errin=0,
            errout=0,
        ),
    )
    monkeypatch.setattr(
        "monitor_agent.collectors.network.psutil.net_connections",
        connection_result,
    )


def test_network_schema_and_ipv6_format(monkeypatch) -> None:
    connection = SimpleNamespace(
        fd=7,
        family=socket.AddressFamily.AF_INET6,
        type=socket.SocketKind.SOCK_STREAM,
        laddr=SimpleNamespace(ip="::1", port=443),
        raddr=SimpleNamespace(ip="2001:db8::1", port=54000),
        status="ESTABLISHED",
        pid=44,
    )
    install_network_fakes(monkeypatch, lambda kind: [connection])
    result = NetworkCollector().collect()
    assert result.data["adapters"] == [
        {
            "interface": "eth0",
            "ipv4": "10.0.0.2",
            "mac": "00:11:22:33:44:55",
            "speed_mb": 1000,
            "mtu": 1500,
        }
    ]
    assert result.data["connections"][0]["laddr"] == "[::1]:443"
    assert result.data["connections"][0]["pid"] == 44


def test_connection_denial_preserves_adapter_and_io(monkeypatch) -> None:
    install_network_fakes(
        monkeypatch,
        lambda kind: (_ for _ in ()).throw(psutil.AccessDenied()),
    )
    result = NetworkCollector().collect()
    assert result.status is CollectorStatus.PARTIAL
    assert result.error_code == "network_connections_denied"
    assert result.data["adapters"]
    assert result.data["connections"] == []
    assert result.data["io"]["bytes_recv"] == 2


def test_disabled_network_never_reads_connections(monkeypatch) -> None:
    monkeypatch.setattr(
        "monitor_agent.collectors.network.psutil.net_connections",
        lambda kind: (_ for _ in ()).throw(AssertionError("executed")),
    )
    result = NetworkCollector(enabled=False).collect()
    assert result.status is CollectorStatus.DISABLED
    assert result.data["connections"] == []
```

- [ ] **Step 2: Write software cache tests**

```python
from monitor_agent.collectors.software import SoftwareCollector


def test_linux_software_results_are_cached(monkeypatch) -> None:
    calls = 0

    def fake_run(command, **kwargs):
        nonlocal calls
        calls += 1
        return "alpha\\t1.0\\nbeta\\t2.0\\n"

    monkeypatch.setattr("monitor_agent.collectors.software._run_command", fake_run)
    monkeypatch.setattr("monitor_agent.collectors.software.sys.platform", "linux")
    collector = SoftwareCollector(cache_ttl_sec=86400)
    assert collector.collect().data == collector.collect().data
    assert calls == 1


def test_disabled_software_collector_does_not_execute(monkeypatch) -> None:
    monkeypatch.setattr(
        "monitor_agent.collectors.software._run_command",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("executed")),
    )
    result = SoftwareCollector(enabled=False).collect()
    assert result.status.value == "disabled"
    assert result.data == {"software": []}
```

- [ ] **Step 3: Run tests and verify the modules are missing**

Run: `python -m pytest tests/collectors/test_network.py tests/collectors/test_software.py -q`

Expected: collection fails for both missing modules.

- [ ] **Step 4: Implement network collection**

Format socket addresses with a helper that brackets IPv6:

```python
def _format_address(address: Any) -> str | None:
    if not address:
        return None
    host = str(address.ip)
    return f"[{host}]:{address.port}" if ":" in host else f"{host}:{address.port}"
```

Use `getattr(psutil, "AF_LINK", object())` for MAC-family comparison. Convert enum-like family and type values using their `name` when present and `str` otherwise. An inaccessible connection table must not suppress adapters or I/O counters.

- [ ] **Step 5: Implement cross-platform software collection and cache**

Use a lock-protected cache storing `(monotonic_timestamp, immutable_records)`. Platform sources are:

- Linux: `dpkg-query -W -f=${Package}\\t${Version}\\n`, then RPM fallback `rpm -qa --queryformat %{NAME}\\t%{VERSION}\\n`; each command has a 20-second timeout.
- Windows: the three uninstall registry paths from the legacy agent; always close opened keys.
- macOS: `/Applications/*.app` plus `brew list --versions` with a 20-second timeout.

Sort and de-duplicate records by case-folded `(name, version, source)`. A missing package manager is not failure when a fallback succeeds. When all applicable sources fail, return `PARTIAL` with code `software_inventory_unavailable`.

- [ ] **Step 6: Run tests and static checks**

Run: `python -m pytest tests/collectors/test_network.py tests/collectors/test_software.py -q`

Run: `ruff check src/monitor_agent/collectors/network.py src/monitor_agent/collectors/software.py tests/collectors`

Run: `mypy src/monitor_agent/collectors/network.py src/monitor_agent/collectors/software.py`

Expected: all commands exit 0.

- [ ] **Step 7: Commit**

```bash
git add src/monitor_agent/collectors/network.py src/monitor_agent/collectors/software.py tests/collectors
git commit -m "feat: collect network and software inventory"
```

---

### Task 8: Schema-Compatible Telemetry Envelope

**Files:**
- Create: `src/monitor_agent/payload.py`
- Create: `tests/test_payload.py`

**Interfaces:**
- Consumes: `MachineIdentity`, `CollectionBatch`, and `CollectorResult`
- Produces: `build_payload(event: str, identity: MachineIdentity, batch: CollectionBatch, *, now: datetime | None = None, event_id: UUID | None = None) -> dict[str, JSONValue]`

- [ ] **Step 1: Write failing schema compatibility test**

```python
from datetime import datetime, timezone
from uuid import UUID

from monitor_agent.identity import MachineIdentity
from monitor_agent.models import CollectionBatch, CollectorResult, CollectorStatus
from monitor_agent.payload import build_payload


def test_payload_preserves_v1_schema_and_adds_agent_metadata() -> None:
    batch = CollectionBatch(
        results=(
            CollectorResult("system", CollectorStatus.SUCCESS, 4, {"system": {"hostname": "host"}}),
            CollectorResult("processes", CollectorStatus.FAILED, 7, {}, "permission_error", "collector access denied"),
        ),
        duration_ms=11,
    )
    payload = build_payload(
        "heartbeat",
        MachineIdentity("machine-uuid", "linux-machine-id"),
        batch,
        now=datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc),
        event_id=UUID("12345678-1234-5678-9234-567812345678"),
    )
    assert payload["schema_version"] == "1.0"
    assert set(
        ["event", "timestamp", "machine_id", "system", "users", "cpu", "memory", "disks", "network", "processes", "software"]
    ).issubset(payload)
    assert payload["event_id"] == "12345678-1234-5678-9234-567812345678"
    assert payload["processes"] == []
    assert payload["agent"]["version"] == "2.0.0"
    assert payload["agent"]["collection_duration_ms"] == 11
    assert payload["agent"]["collectors"]["processes"]["status"] == "failed"
```

- [ ] **Step 2: Run test and verify payload module is missing**

Run: `python -m pytest tests/test_payload.py -q`

Expected: collection fails because `monitor_agent.payload` does not exist.

- [ ] **Step 3: Implement envelope construction**

Start from these exact defaults so failed collectors preserve schema:

```python
sections: dict[str, JSONValue] = {
    "system": {},
    "users": [],
    "cpu": {},
    "memory": {},
    "disks": [],
    "network": {"adapters": [], "connections": [], "io": {}},
    "processes": [],
    "software": [],
}
```

Merge only recognized top-level section keys from successful or partial collector mappings. Reject event names that do not match `^[a-z][a-z0-9_-]{0,63}$` with `ValueError("invalid event name")`. Normalize naive `now` values by rejecting them; emitted timestamps must be UTC-aware ISO 8601 strings. Generate one UUID4 when `event_id` is absent.

The `agent` object contains `version`, `python`, `platform`, `collection_duration_ms`, `identity_source`, and a collector map containing `status`, `duration_ms`, `error_code`, and `error_message`.

- [ ] **Step 4: Run payload tests and static checks**

Run: `python -m pytest tests/test_payload.py -q`

Run: `ruff check src/monitor_agent/payload.py tests/test_payload.py`

Run: `mypy src/monitor_agent/payload.py`

Expected: all commands exit 0.

- [ ] **Step 5: Commit**

```bash
git add src/monitor_agent/payload.py tests/test_payload.py
git commit -m "feat: preserve telemetry schema contract"
```

---

### Task 9: Atomic Bounded Offline Spool

**Files:**
- Modify: `src/monitor_agent/models.py`
- Create: `src/monitor_agent/spool.py`
- Create: `tests/test_spool.py`

**Interfaces:**
- Produces: `SpoolStats(pending_count: int, pending_bytes: int, dead_letter_count: int)`
- Produces: `RetentionResult(evicted_count: int, evicted_bytes: int)`
- Produces: `Spool(root: Path, max_bytes: int, max_age_sec: int)`
- Produces: `enqueue(payload: Mapping[str, JSONValue], *, now: datetime | None = None) -> Path`
- Produces: `pending() -> list[Path]` and `load(path: Path) -> dict[str, JSONValue] | None`
- Produces: `ack(path: Path) -> None` and `reject(path: Path) -> Path`
- Produces: `enforce_retention(*, now: datetime | None = None) -> RetentionResult` and `stats() -> SpoolStats`

- [ ] **Step 1: Write failing atomicity and ordering tests**

```python
from pathlib import Path

from monitor_agent.spool import Spool


def payload(event_id: str) -> dict[str, object]:
    return {"schema_version": "1.0", "event_id": event_id, "event": "heartbeat"}


def test_enqueue_is_owner_only_and_oldest_first(tmp_path: Path) -> None:
    spool = Spool(tmp_path, max_bytes=1_048_576, max_age_sec=3600)
    first = spool.enqueue(payload("00000000-0000-4000-8000-000000000001"))
    second = spool.enqueue(payload("00000000-0000-4000-8000-000000000002"))
    assert spool.pending() == [first, second]
    assert first.stat().st_mode & 0o777 == 0o600
    assert spool.load(first)["event_id"].endswith("0001")
    assert not list(tmp_path.glob("*.tmp"))


def test_corrupt_record_moves_to_dead_letter(tmp_path: Path) -> None:
    spool = Spool(tmp_path, max_bytes=1_048_576, max_age_sec=3600)
    broken = tmp_path / "20260720T120000000000Z_broken.json"
    broken.write_text("{", encoding="utf-8")
    assert spool.load(broken) is None
    assert not broken.exists()
    assert len(list((tmp_path / "dead-letter").glob("*.json"))) == 1


def test_retention_evicts_oldest_records_by_size(tmp_path: Path) -> None:
    spool = Spool(tmp_path, max_bytes=180, max_age_sec=3600)
    spool.enqueue(payload("00000000-0000-4000-8000-000000000001"))
    spool.enqueue(payload("00000000-0000-4000-8000-000000000002"))
    result = spool.enforce_retention()
    assert result.evicted_count >= 1
    assert spool.stats().pending_bytes <= 180
```

- [ ] **Step 2: Run tests and verify spool module is missing**

Run: `python -m pytest tests/test_spool.py -q`

Expected: collection fails because `monitor_agent.spool` does not exist.

- [ ] **Step 3: Add spool result dataclasses**

```python
@dataclass(frozen=True, slots=True)
class SpoolStats:
    pending_count: int
    pending_bytes: int
    dead_letter_count: int


@dataclass(frozen=True, slots=True)
class RetentionResult:
    evicted_count: int
    evicted_bytes: int
```

- [ ] **Step 4: Implement atomic spool operations**

Use this filename function:

```python
def _record_name(payload: Mapping[str, JSONValue], now: datetime) -> str:
    timestamp = now.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    event_id = str(payload["event_id"])
    UUID(event_id)
    return f"{timestamp}_{event_id}.json"
```

Serialize with `json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)`. Within a `threading.RLock`:

1. Create the spool and dead-letter directories with mode `0o700`.
2. Write bytes to `tempfile.NamedTemporaryFile(dir=root, delete=False)`.
3. Flush, `os.fsync`, and `os.chmod(temp_path, 0o600)`.
4. Replace the destination with `os.replace`.
5. Open and fsync the directory on POSIX.
6. Enforce retention.

`load` validates that decoded JSON is a mapping with a UUID `event_id`. Invalid records move to dead letter using `os.replace` and return `None`. `reject` moves a valid record to dead letter with a `.rejected.json` suffix. Retention excludes the dead-letter directory, deletes records older than the cutoff first, then oldest records until total bytes are within the cap.

- [ ] **Step 5: Run spool tests and static checks**

Run: `python -m pytest tests/test_spool.py -q`

Run: `ruff check src/monitor_agent/models.py src/monitor_agent/spool.py tests/test_spool.py`

Run: `mypy src/monitor_agent/models.py src/monitor_agent/spool.py`

Expected: all commands exit 0.

- [ ] **Step 6: Commit**

```bash
git add src/monitor_agent/models.py src/monitor_agent/spool.py tests/test_spool.py
git commit -m "feat: persist bounded telemetry spool"
```

---

### Task 10: Classified HTTP Transport With Backoff

**Files:**
- Modify: `src/monitor_agent/models.py`
- Create: `src/monitor_agent/transport.py`
- Create: `tests/test_transport.py`

**Interfaces:**
- Produces: `DeliveryKind` enum values `success`, `retriable`, `authentication`, and `permanent`
- Produces: `DeliveryResult(kind: DeliveryKind, status_code: int | None, attempts: int, message: str)`
- Produces: `TelemetryTransport(config: AgentConfig, *, session: requests.Session | None = None, max_attempts: int = 4, sleep: Callable[[float], None] = time.sleep, random_value: Callable[[], float] = random.random)`
- Produces: `TelemetryTransport.send(payload: Mapping[str, JSONValue]) -> DeliveryResult`

- [ ] **Step 1: Write failing response-classification tests**

```python
from collections.abc import Iterator
from types import SimpleNamespace

import requests

from monitor_agent.config import load_config
from monitor_agent.models import DeliveryKind
from monitor_agent.transport import TelemetryTransport


class FakeSession:
    def __init__(self, outcomes: Iterator[object]) -> None:
        self.outcomes = outcomes
        self.calls = []

    def post(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        outcome = next(self.outcomes)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    def close(self) -> None:
        return None


def response(status: int, headers: dict[str, str] | None = None):
    return SimpleNamespace(status_code=status, headers=headers or {})


def config():
    return load_config(
        {
            "MONITOR_COLLECTOR_URI": "https://collector.internal/api/v1/telemetry",
            "MONITOR_API_TOKEN": "token-value",
        },
        platform_name="linux",
    )


def telemetry():
    return {"event_id": "12345678-1234-4678-9234-567812345678", "event": "heartbeat"}


def test_success_uses_json_timeout_tls_and_safe_headers() -> None:
    session = FakeSession(iter([response(202)]))
    result = TelemetryTransport(config(), session=session).send(telemetry())
    assert result.kind is DeliveryKind.SUCCESS
    _, kwargs = session.calls[0]
    assert kwargs["json"] == telemetry()
    assert kwargs["timeout"] == (5.0, 15.0)
    assert kwargs["verify"] is True
    assert kwargs["headers"]["Idempotency-Key"] == telemetry()["event_id"]
    assert kwargs["headers"]["User-Agent"] == "monitor-agent/2.0.0"


def test_retriable_status_backs_off_then_succeeds() -> None:
    delays = []
    session = FakeSession(iter([response(503), response(200)]))
    result = TelemetryTransport(
        config(),
        session=session,
        sleep=delays.append,
        random_value=lambda: 0.5,
    ).send(telemetry())
    assert result.kind is DeliveryKind.SUCCESS
    assert result.attempts == 2
    assert delays == [0.25]


def test_authentication_is_not_retried() -> None:
    session = FakeSession(iter([response(401)]))
    result = TelemetryTransport(config(), session=session).send(telemetry())
    assert result.kind is DeliveryKind.AUTHENTICATION
    assert result.attempts == 1


def test_permanent_client_error_is_not_retried() -> None:
    session = FakeSession(iter([response(422)]))
    result = TelemetryTransport(config(), session=session).send(telemetry())
    assert result.kind is DeliveryKind.PERMANENT


def test_timeout_exhaustion_is_retriable() -> None:
    session = FakeSession(iter([requests.Timeout(), requests.Timeout()]))
    result = TelemetryTransport(
        config(),
        session=session,
        max_attempts=2,
        sleep=lambda delay: None,
    ).send(telemetry())
    assert result.kind is DeliveryKind.RETRIABLE
    assert result.attempts == 2
```

- [ ] **Step 2: Run tests and verify transport symbols are missing**

Run: `python -m pytest tests/test_transport.py -q`

Expected: collection fails because `monitor_agent.transport` does not exist.

- [ ] **Step 3: Add delivery contracts to `models.py`**

```python
class DeliveryKind(StrEnum):
    SUCCESS = "success"
    RETRIABLE = "retriable"
    AUTHENTICATION = "authentication"
    PERMANENT = "permanent"


@dataclass(frozen=True, slots=True)
class DeliveryResult:
    kind: DeliveryKind
    status_code: int | None
    attempts: int
    message: str
```

- [ ] **Step 4: Implement transport classification and bounded backoff**

Send to `config.collector_uri` with headers:

```python
{
    "Accept": "application/json",
    "Authorization": f"Bearer {config.api_token}",
    "Content-Type": "application/json",
    "Idempotency-Key": str(payload["event_id"]),
    "User-Agent": f"monitor-agent/{__version__}",
    "X-Event-Type": str(payload["event"]),
    "X-Machine-ID": str(payload["machine_id"]),
    "X-Schema-Ver": str(payload["schema_version"]),
}
```

Classify HTTP `200-299` as success, `401/403` as authentication, `408/425/429/500-599` as retriable, and other `400-499` as permanent. Treat `requests.Timeout` and `requests.ConnectionError` as retriable. Other `requests.RequestException` values are retriable with fixed message `request failed`.

Backoff before retry number `n` is `min(30.0, 0.5 * 2 ** (n - 1)) * random_value()`. Parse numeric and HTTP-date `Retry-After` values, clamp them to `0-60` seconds, and prefer the larger of server delay and jittered delay. Never include response bodies, headers, URI credentials, payloads, or exception strings in `DeliveryResult.message`.

- [ ] **Step 5: Run transport tests and static checks**

Run: `python -m pytest tests/test_transport.py -q`

Run: `ruff check src/monitor_agent/models.py src/monitor_agent/transport.py tests/test_transport.py`

Run: `mypy src/monitor_agent/models.py src/monitor_agent/transport.py`

Expected: all commands exit 0.

- [ ] **Step 6: Commit**

```bash
git add src/monitor_agent/models.py src/monitor_agent/transport.py tests/test_transport.py
git commit -m "feat: classify telemetry delivery failures"
```

---

### Task 11: Runtime Replay, Scheduling, and Graceful Shutdown

**Files:**
- Create: `src/monitor_agent/runtime.py`
- Create: `tests/test_runtime.py`

**Interfaces:**
- Consumes: `AgentConfig`, `MachineIdentity`, `Collector`, `Spool`, and `TelemetryTransport`
- Produces: `CycleResult(event_id: str, delivered: bool, spooled: bool, delivery_kind: DeliveryKind | None)`
- Produces: `AgentRuntime` with `run_cycle`, `replay`, `run`, and `request_stop`

- [ ] **Step 1: Write failing replay-order and failure tests**

```python
from pathlib import Path
from threading import Event

from monitor_agent.config import load_config
from monitor_agent.identity import MachineIdentity
from monitor_agent.models import CollectorPayload, DeliveryKind, DeliveryResult
from monitor_agent.runtime import AgentRuntime
from monitor_agent.spool import Spool


class EmptyCollector:
    name = "empty"

    def collect(self) -> CollectorPayload:
        return CollectorPayload(data={})


class FakeTransport:
    def __init__(self, results: list[DeliveryResult]) -> None:
        self.results = iter(results)
        self.event_ids: list[str] = []

    def send(self, payload):
        self.event_ids.append(str(payload["event_id"]))
        return next(self.results)

    def close(self) -> None:
        return None


def runtime(tmp_path: Path, transport: FakeTransport, replay_batch_size: int = 20) -> AgentRuntime:
    config = load_config(
        {
            "MONITOR_COLLECTOR_URI": "https://collector.internal/api/v1/telemetry",
            "MONITOR_API_TOKEN": "token",
            "MONITOR_SPOOL_PATH": str(tmp_path),
            "MONITOR_REPLAY_BATCH_SIZE": str(replay_batch_size),
        },
        platform_name="linux",
    )
    return AgentRuntime(
        config=config,
        identity=MachineIdentity("machine-id", "test"),
        collectors=[EmptyCollector()],
        transport=transport,
        spool=Spool(tmp_path, config.spool_max_bytes, config.spool_max_age_sec),
        stop_event=Event(),
    )


def test_backlog_is_sent_before_live_event(tmp_path: Path) -> None:
    transport = FakeTransport(
        [
            DeliveryResult(DeliveryKind.SUCCESS, 200, 1, "delivered"),
            DeliveryResult(DeliveryKind.SUCCESS, 200, 1, "delivered"),
        ]
    )
    agent = runtime(tmp_path, transport)
    queued = agent.spool.enqueue(
        {"schema_version": "1.0", "event_id": "12345678-1234-4678-9234-567812345678", "event": "heartbeat"}
    )
    result = agent.run_cycle("heartbeat")
    assert transport.event_ids[0].endswith("5678")
    assert not queued.exists()
    assert result.delivered is True


def test_remaining_backlog_spools_live_event_without_sending_it(tmp_path: Path) -> None:
    transport = FakeTransport([DeliveryResult(DeliveryKind.SUCCESS, 200, 1, "delivered")])
    agent = runtime(tmp_path, transport, replay_batch_size=1)
    for suffix in ("0001", "0002"):
        agent.spool.enqueue(
            {
                "schema_version": "1.0",
                "event_id": f"12345678-1234-4678-9234-56781234{suffix}",
                "event": "heartbeat",
            }
        )
    result = agent.run_cycle("heartbeat")
    assert result.spooled is True
    assert len(transport.event_ids) == 1
    assert len(agent.spool.pending()) == 2


def test_authentication_failure_keeps_replay_record(tmp_path: Path) -> None:
    transport = FakeTransport(
        [DeliveryResult(DeliveryKind.AUTHENTICATION, 401, 1, "authentication rejected")]
    )
    agent = runtime(tmp_path, transport)
    record = agent.spool.enqueue(
        {"schema_version": "1.0", "event_id": "12345678-1234-4678-9234-567812345678", "event": "heartbeat"}
    )
    assert agent.replay() is False
    assert record.exists()
```

- [ ] **Step 2: Write monotonic shutdown test**

```python
class ScriptedEvent:
    def __init__(self) -> None:
        self.waits: list[float] = []
        self.set_called = False

    def wait(self, timeout: float) -> bool:
        self.waits.append(timeout)
        return len(self.waits) >= 3

    def set(self) -> None:
        self.set_called = True

    def is_set(self) -> bool:
        return self.set_called


def test_run_waits_startup_then_heartbeat_and_stops(monkeypatch, tmp_path: Path) -> None:
    event = ScriptedEvent()
    transport = FakeTransport(
        [
            DeliveryResult(DeliveryKind.SUCCESS, 200, 1, "delivered"),
            DeliveryResult(DeliveryKind.SUCCESS, 200, 1, "delivered"),
        ]
    )
    agent = runtime(tmp_path, transport)
    agent.stop_event = event
    monkeypatch.setattr("monitor_agent.runtime.time.monotonic", lambda: 100.0)
    events = []
    monkeypatch.setattr(agent, "run_cycle", lambda name: events.append(name))
    agent.run()
    assert events == ["startup", "heartbeat"]
    assert event.waits[0] == agent.config.startup_delay_sec


def test_request_stop_sets_event(tmp_path: Path) -> None:
    event = Event()
    agent = runtime(
        tmp_path,
        FakeTransport([DeliveryResult(DeliveryKind.SUCCESS, 200, 1, "delivered")]),
    )
    agent.stop_event = event
    agent.request_stop()
    assert event.is_set()
```

- [ ] **Step 3: Run tests and verify runtime module is missing**

Run: `python -m pytest tests/test_runtime.py -q`

Expected: collection fails because `monitor_agent.runtime` does not exist.

- [ ] **Step 4: Add `CycleResult` and implement replay**

Add this frozen slotted dataclass to `models.py`:

```python
@dataclass(frozen=True, slots=True)
class CycleResult:
    event_id: str
    delivered: bool
    spooled: bool
    delivery_kind: DeliveryKind | None
```

`AgentRuntime.replay()` loads at most `replay_batch_size` pending files in oldest-first order:

- Success: acknowledge and continue.
- Permanent: reject to dead letter and continue.
- Authentication or retriable: leave the record in place and stop replay.
- Corrupt load: `Spool.load` already moves it; continue.

Return `True` only when no pending records remain.

- [ ] **Step 5: Implement collection and live delivery**

`run_cycle(event)` calls `collect_all`, `build_payload`, and `replay`. When replay leaves backlog, enqueue the live payload without transmitting it. Otherwise send live:

- Success returns delivered.
- Retriable or authentication enqueues and returns spooled.
- Permanent returns neither delivered nor spooled and logs only event ID/status.

Call `Spool.enforce_retention()` after enqueue/replay.

- [ ] **Step 6: Implement monotonic runtime loop**

`run()` waits `startup_delay_sec` using `stop_event.wait`, returns immediately when stopped, sends one `startup` cycle, then schedules heartbeats from `time.monotonic()`. After each cycle, advance the next deadline by exact heartbeat intervals until it is in the future so long collections do not create a tight catch-up loop. `request_stop()` sets the event. Always close the transport in a `finally` block.

- [ ] **Step 7: Run runtime tests and static checks**

Run: `python -m pytest tests/test_runtime.py -q`

Run: `ruff check src/monitor_agent/models.py src/monitor_agent/runtime.py tests/test_runtime.py`

Run: `mypy src/monitor_agent/models.py src/monitor_agent/runtime.py`

Expected: all commands exit 0.

- [ ] **Step 8: Commit**

```bash
git add src/monitor_agent/models.py src/monitor_agent/runtime.py tests/test_runtime.py
git commit -m "feat: schedule durable telemetry delivery"
```

---

### Task 12: Structured Logging and Operational CLI

**Files:**
- Create: `src/monitor_agent/logging_setup.py`
- Modify: `src/monitor_agent/cli.py`
- Modify: `src/monitor_agent/collectors/__init__.py`
- Create: `tests/test_logging_setup.py`
- Create: `tests/test_cli.py`

**Interfaces:**
- Produces: `configure_logging(config: AgentConfig) -> None`
- Produces: `build_collectors(config: AgentConfig, identity: MachineIdentity) -> list[Collector]`
- Expands CLI with `run`, `once`, `check-config`, `health`, and `version`
- Defines exit codes: 0 success, 2 configuration failure, 3 collection failure, 4 transport failure

- [ ] **Step 1: Write secret-safe logging tests**

```python
import json
import logging

from monitor_agent.config import load_config
from monitor_agent.logging_setup import JsonFormatter, SecretFilter


def test_secret_filter_masks_token_and_authorization_header() -> None:
    record = logging.LogRecord(
        "monitor_agent",
        logging.ERROR,
        __file__,
        10,
        "request failed token=%s Authorization=%s",
        ("raw-token", "Bearer raw-token"),
        None,
    )
    filtered = SecretFilter(["raw-token"]).filter(record)
    assert filtered is True
    rendered = record.getMessage()
    assert "raw-token" not in rendered
    assert "***" in rendered


def test_json_formatter_emits_utc_structured_fields() -> None:
    record = logging.LogRecord("monitor_agent.transport", logging.INFO, __file__, 4, "sent", (), None)
    parsed = json.loads(JsonFormatter().format(record))
    assert parsed["level"] == "INFO"
    assert parsed["component"] == "monitor_agent.transport"
    assert parsed["message"] == "sent"
    assert parsed["timestamp"].endswith("Z")
```

- [ ] **Step 2: Write CLI behavior tests**

```python
import json
from types import SimpleNamespace

from monitor_agent import cli
from monitor_agent.config import ConfigError


def test_parser_exposes_every_operational_command() -> None:
    parser = cli.build_parser()
    assert parser.parse_args(["run"]).command == "run"
    assert parser.parse_args(["once", "--no-transmit"]).no_transmit is True
    assert parser.parse_args(["check-config"]).command == "check-config"
    assert parser.parse_args(["health"]).command == "health"
    assert parser.parse_args(["version"]).command == "version"


def test_config_error_uses_exit_two_without_secret(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        cli,
        "load_config",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            ConfigError("MONITOR_API_TOKEN is required")
        ),
    )
    assert cli.main(["check-config"]) == 2
    captured = capsys.readouterr()
    assert "MONITOR_API_TOKEN is required" in captured.err
    assert "raw-token" not in captured.err


def test_once_no_transmit_prints_payload_without_transport(monkeypatch, capsys) -> None:
    payload = {
        "schema_version": "1.0",
        "event_id": "12345678-1234-4678-9234-567812345678",
        "event": "heartbeat",
    }
    monkeypatch.setattr(cli, "_collect_once", lambda config, event: (payload, False))
    monkeypatch.setattr(
        cli,
        "TelemetryTransport",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("transport created")),
    )
    assert cli.main(["once", "--event", "heartbeat", "--no-transmit"]) == 0
    assert json.loads(capsys.readouterr().out) == payload


def test_health_hides_machine_id_and_token(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        cli,
        "_health_snapshot",
        lambda config: {
            "version": "2.0.0",
            "python": "3.14.6",
            "identity_source": "linux-machine-id",
            "spool": {"pending_count": 0, "pending_bytes": 0, "dead_letter_count": 0},
            "collectors": ["system", "users", "resources", "network", "processes", "software"],
        },
    )
    assert cli.main(["health"]) == 0
    output = capsys.readouterr().out
    assert "linux-machine-id" in output
    assert "raw-machine-id" not in output
    assert "raw-token" not in output


def test_run_installs_stop_signal_handlers(monkeypatch) -> None:
    handlers = {}
    fake_runtime = SimpleNamespace(run=lambda: None, request_stop=lambda: None)
    monkeypatch.setattr(cli, "_create_runtime", lambda config: fake_runtime)
    monkeypatch.setattr(cli.signal, "signal", lambda signum, handler: handlers.setdefault(signum, handler))
    assert cli.main(["run"]) == 0
    assert cli.signal.SIGINT in handlers
    assert cli.signal.SIGTERM in handlers
```

- [ ] **Step 3: Run tests and verify commands are absent**

Run: `python -m pytest tests/test_logging_setup.py tests/test_cli.py -q`

Expected: failures identify missing logging classes and CLI subcommands.

- [ ] **Step 4: Implement text/JSON logging**

`SecretFilter` receives secret strings, replaces each occurrence in `record.msg` and string arguments, and masks case-insensitive `Authorization: Bearer` patterns. `JsonFormatter` emits `timestamp`, `level`, `component`, `message`, and optional `event_id` using compact JSON.

`configure_logging` clears only handlers owned by `monitor_agent`, sets the configured level, and chooses stdout when `log_path is None`. Otherwise use `RotatingFileHandler(maxBytes=10_485_760, backupCount=5, encoding="utf-8")` and create its directory with mode `0o700`. Attach `SecretFilter` containing the API token when present.

- [ ] **Step 5: Build the collector registry**

Return collectors in this fixed order:

```python
[
    SystemCollector(identity),
    UsersCollector(),
    ResourceCollector(),
    NetworkCollector(config.include_network_connections),
    ProcessesCollector(config.process_cmdline_mode),
    SoftwareCollector(config.include_software),
]
```

Fixed ordering keeps payload status and tests deterministic.

- [ ] **Step 6: Expand CLI and assemble dependencies**

Parser shapes:

```text
monitor-agent run
monitor-agent once [--event EVENT] [--no-transmit]
monitor-agent check-config
monitor-agent health
monitor-agent version
```

`run` and transmitting `once` call `load_config(require_transport=True)`. `once --no-transmit` calls `load_config(require_transport=False)`. `check-config` validates credentials, creates the spool parent, verifies write access by atomically creating and deleting an owner-only probe file, validates CA/log paths, then prints `configuration valid`. `health` performs the same checks and calls collectors only for availability status, never prints telemetry.

Keep these private orchestration helpers so CLI routing remains independently testable:

```python
def _create_runtime(config: AgentConfig) -> AgentRuntime
def _collect_once(config: AgentConfig, event: str) -> tuple[dict[str, JSONValue], bool]
def _health_snapshot(config: AgentConfig) -> dict[str, JSONValue]
def _validate_paths(config: AgentConfig) -> None
```

Catch `ConfigError` at the command boundary and use exit code 2. Use exit code 3 when every collector fails or times out. Use exit code 4 for non-success transmission. Register signal handlers only for `run`.

- [ ] **Step 7: Run CLI/logging tests and static checks**

Run: `python -m pytest tests/test_logging_setup.py tests/test_cli.py -q`

Run: `ruff check src/monitor_agent/cli.py src/monitor_agent/logging_setup.py src/monitor_agent/collectors tests/test_cli.py tests/test_logging_setup.py`

Run: `mypy src/monitor_agent/cli.py src/monitor_agent/logging_setup.py`

Expected: all commands exit 0.

- [ ] **Step 8: Commit**

```bash
git add src/monitor_agent/cli.py src/monitor_agent/logging_setup.py src/monitor_agent/collectors/__init__.py tests/test_cli.py tests/test_logging_setup.py
git commit -m "feat: expose operational agent CLI"
```

---

### Task 13: Legacy Compatibility, Locked Dependencies, and Package Build

**Files:**
- Modify: `agent/monitor_agent.py`
- Modify: `agent/requirements.txt`
- Create: `requirements.lock`
- Create: `tests/test_legacy_entrypoint.py`

**Interfaces:**
- Consumes: `monitor_agent.cli.entrypoint`
- Produces: old script path as a compatibility launcher
- Produces: complete hash-pinned deployment lock

- [ ] **Step 1: Write failing legacy entry-point test**

```python
import runpy


def test_legacy_script_delegates_to_packaged_entrypoint(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr("monitor_agent.cli.entrypoint", lambda: calls.append("called"))
    runpy.run_path("agent/monitor_agent.py", run_name="__main__")
    assert calls == ["called"]
```

- [ ] **Step 2: Run the test and verify the legacy agent still starts collection**

Run: `python -m pytest tests/test_legacy_entrypoint.py -q`

Expected: test fails because the legacy script imports runtime dependencies and invokes its old main loop.

- [ ] **Step 3: Replace the legacy file with a compatibility shim**

```python
#!/usr/bin/env python3
"""Compatibility entry point for monitor-agent 2.x."""

from monitor_agent.cli import entrypoint


if __name__ == "__main__":
    entrypoint()
```

Set `agent/requirements.txt` to:

```requirements
psutil==7.2.2
requests==2.34.2
```

- [ ] **Step 4: Generate and verify the deployment lock**

Run: `python -m piptools compile --generate-hashes --resolver=backtracking --output-file=requirements.lock pyproject.toml`

Expected: `requirements.lock` contains exact versions and hashes for `certifi`, `charset-normalizer`, `idna`, `psutil`, `requests`, and `urllib3` with no editable or local-path entries.

Run: `python -m pip install --dry-run --require-hashes -r requirements.lock`

Expected: dependency resolution succeeds.

- [ ] **Step 5: Build and inspect distributions**

Run: `python -m build`

Expected: one wheel and one source distribution appear under `dist/`.

Run: `python -m pip install --force-reinstall --no-deps dist/monitor_agent-2.0.0-py3-none-any.whl`

Run: `monitor-agent version`

Expected: `monitor-agent 2.0.0`.

- [ ] **Step 6: Run compatibility test and full package smoke test**

Run: `python -m pytest tests/test_legacy_entrypoint.py tests/test_package.py -q`

Expected: all tests pass.

- [ ] **Step 7: Commit**

```bash
git add agent/monitor_agent.py agent/requirements.txt requirements.lock tests/test_legacy_entrypoint.py
git commit -m "build: lock monitor agent runtime"
```

---

### Task 14: Hardened Linux Deployment

**Files:**
- Modify: `deploy/linux/monitor-agent.service`
- Create: `deploy/linux/install.sh`
- Create: `deploy/linux/uninstall.sh`
- Create: `deploy/linux/monitor-agent.env.example`
- Create: `tests/deploy/test_linux.py`

**Interfaces:**
- Consumes: built wheel, `requirements.lock`, and environment file
- Produces: idempotent `systemd` install, upgrade, and uninstall
- Produces: service entry point `/opt/monitor-agent/venv/bin/monitor-agent run`

- [ ] **Step 1: Write failing Linux deployment tests**

```python
from pathlib import Path


SERVICE = Path("deploy/linux/monitor-agent.service")


def test_service_uses_protected_environment_file_and_packaged_cli() -> None:
    text = SERVICE.read_text(encoding="utf-8")
    assert "EnvironmentFile=/etc/monitor-agent/monitor-agent.env" in text
    assert "MONITOR_API_TOKEN=" not in text
    assert "ExecStart=/opt/monitor-agent/venv/bin/monitor-agent run" in text


def test_service_has_required_hardening() -> None:
    text = SERVICE.read_text(encoding="utf-8")
    for directive in (
        "NoNewPrivileges=true",
        "ProtectSystem=strict",
        "ProtectHome=read-only",
        "PrivateTmp=true",
        "RestrictSUIDSGID=true",
        "LockPersonality=true",
        "MemoryDenyWriteExecute=true",
        "UMask=0077",
        "ReadWritePaths=/var/lib/monitor-agent",
    ):
        assert directive in text


def test_install_scripts_are_strict_and_never_delete_state_by_default() -> None:
    install = Path("deploy/linux/install.sh").read_text(encoding="utf-8")
    uninstall = Path("deploy/linux/uninstall.sh").read_text(encoding="utf-8")
    assert "set -eu" in install
    assert "pip install --require-hashes" in install
    assert "/var/lib/monitor-agent" not in uninstall.split("--purge", 1)[0]
```

- [ ] **Step 2: Run tests and capture legacy deployment failures**

Run: `python -m pytest tests/deploy/test_linux.py -q`

Expected: failures show inline token configuration, legacy script entry point, missing hardening, and missing scripts.

- [ ] **Step 3: Replace the Linux service**

```ini
[Unit]
Description=Monitor Agent 2.0 Endpoint Telemetry
Documentation=file:/opt/monitor-agent/README.md
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
EnvironmentFile=/etc/monitor-agent/monitor-agent.env
ExecStartPre=/opt/monitor-agent/venv/bin/monitor-agent check-config
ExecStart=/opt/monitor-agent/venv/bin/monitor-agent run
Restart=on-failure
RestartSec=30
TimeoutStopSec=45
StateDirectory=monitor-agent
StateDirectoryMode=0700
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=read-only
PrivateTmp=true
PrivateDevices=true
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
RestrictSUIDSGID=true
LockPersonality=true
MemoryDenyWriteExecute=true
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6
SystemCallArchitectures=native
UMask=0077
ReadWritePaths=/var/lib/monitor-agent
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

Keep the service running as root because cross-user process and connection visibility is an explicit agent requirement; rely on systemd confinement rather than pretending an unprivileged account can read the same telemetry.

- [ ] **Step 4: Add the Linux installer**

```bash
#!/usr/bin/env bash
set -eu

if [ "$(id -u)" -ne 0 ]; then
    echo "install.sh must run as root" >&2
    exit 2
fi

if [ "$#" -ne 2 ]; then
    echo "usage: install.sh WHEEL_PATH ENV_FILE" >&2
    exit 2
fi

WHEEL_PATH=$1
ENV_SOURCE=$2
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PROJECT_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/../.." && pwd)
INSTALL_DIR=/opt/monitor-agent
CONFIG_DIR=/etc/monitor-agent

test -f "$WHEEL_PATH"
test -f "$ENV_SOURCE"
test -f "$PROJECT_DIR/requirements.lock"

install -d -m 0755 "$INSTALL_DIR"
install -d -m 0700 "$CONFIG_DIR"
python3.14 -m venv "$INSTALL_DIR/venv"
"$INSTALL_DIR/venv/bin/python" -m pip install --upgrade pip
"$INSTALL_DIR/venv/bin/pip" install --require-hashes -r "$PROJECT_DIR/requirements.lock"
"$INSTALL_DIR/venv/bin/pip" install --no-deps --force-reinstall "$WHEEL_PATH"
install -m 0600 "$ENV_SOURCE" "$CONFIG_DIR/monitor-agent.env"
install -m 0644 "$SCRIPT_DIR/monitor-agent.service" /etc/systemd/system/monitor-agent.service
install -m 0644 "$PROJECT_DIR/README.md" "$INSTALL_DIR/README.md"

"$INSTALL_DIR/venv/bin/monitor-agent" check-config
systemctl daemon-reload
systemctl enable --now monitor-agent.service
systemctl --no-pager --full status monitor-agent.service
```

Add `uninstall.sh`:

```bash
#!/usr/bin/env bash
set -eu

if [ "$(id -u)" -ne 0 ]; then
    echo "uninstall.sh must run as root" >&2
    exit 2
fi

PURGE=false
if [ "$#" -gt 1 ]; then
    echo "usage: uninstall.sh [--purge]" >&2
    exit 2
fi
if [ "$#" -eq 1 ]; then
    if [ "$1" != "--purge" ]; then
        echo "usage: uninstall.sh [--purge]" >&2
        exit 2
    fi
    PURGE=true
fi

systemctl disable --now monitor-agent.service 2>/dev/null || true
rm -f /etc/systemd/system/monitor-agent.service
rm -rf /opt/monitor-agent
systemctl daemon-reload

if [ "$PURGE" = true ]; then
    echo "Removing /etc/monitor-agent and /var/lib/monitor-agent"
    rm -rf /etc/monitor-agent /var/lib/monitor-agent
else
    echo "Preserved /etc/monitor-agent and /var/lib/monitor-agent"
fi
```

- [ ] **Step 5: Add a safe environment example**

```dotenv
MONITOR_COLLECTOR_URI=https://collector.internal/api/v1/telemetry
MONITOR_API_TOKEN=replace-with-managed-secret
MONITOR_HEARTBEAT_SEC=300
MONITOR_STARTUP_DELAY_SEC=30
MONITOR_SPOOL_PATH=/var/lib/monitor-agent/spool
MONITOR_PROCESS_CMDLINE_MODE=redacted
MONITOR_LOG_FORMAT=json
```

The example is never installed as live configuration.

- [ ] **Step 6: Validate service, shell, and tests**

Run: `systemd-analyze verify deploy/linux/monitor-agent.service`

Expected: exit 0 with no unknown directives.

Run: `bash -n deploy/linux/install.sh deploy/linux/uninstall.sh`

Run: `python -m pytest tests/deploy/test_linux.py -q`

Expected: all commands exit 0.

- [ ] **Step 7: Commit**

```bash
git add deploy/linux tests/deploy/test_linux.py
git commit -m "deploy: harden Linux monitor agent service"
```

---

### Task 15: Validated Windows Deployment

**Files:**
- Modify: `deploy/windows/monitor_agent_task.xml`
- Create: `deploy/windows/run-agent.ps1`
- Create: `deploy/windows/install.ps1`
- Create: `deploy/windows/uninstall.ps1`
- Create: `deploy/windows/monitor-agent.env.example`
- Create: `tests/deploy/test_windows.py`

**Interfaces:**
- Consumes: built wheel, lock file, and protected environment file
- Produces: Task Scheduler job `MonitorAgent` running as LocalSystem
- Produces: deterministic install root `C:\ProgramData\MonitorAgent`

- [ ] **Step 1: Write failing Windows deployment tests**

```python
from pathlib import Path
from xml.etree import ElementTree


TASK_XML = Path("deploy/windows/monitor_agent_task.xml")


def test_task_xml_parses_and_declaration_is_first() -> None:
    raw = TASK_XML.read_bytes()
    assert raw.startswith(b'<?xml version="1.0" encoding="UTF-8"?>')
    root = ElementTree.fromstring(raw)
    assert root.tag.endswith("Task")


def test_task_uses_launcher_and_restarts() -> None:
    text = TASK_XML.read_text(encoding="utf-8")
    assert r"C:\ProgramData\MonitorAgent\run-agent.ps1" in text
    assert r"C:\Python311" not in text
    assert "<RestartOnFailure>" in text
    assert "<UserId>S-1-5-18</UserId>" in text


def test_installer_locks_configuration_acl() -> None:
    text = Path("deploy/windows/install.ps1").read_text(encoding="utf-8")
    assert "icacls" in text
    assert "*S-1-5-18:(OI)(CI)F" in text
    assert "*S-1-5-32-544:(OI)(CI)F" in text
```

- [ ] **Step 2: Run tests and reproduce invalid XML**

Run: `python -m pytest tests/deploy/test_windows.py -q`

Expected: XML parsing fails because comments precede the declaration and installer files do not exist.

- [ ] **Step 3: Replace the scheduled-task XML**

```xml
<?xml version="1.0" encoding="UTF-8"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>Monitor Agent 2.0 endpoint telemetry</Description>
  </RegistrationInfo>
  <Triggers>
    <BootTrigger>
      <Enabled>true</Enabled>
      <Delay>PT30S</Delay>
    </BootTrigger>
  </Triggers>
  <Principals>
    <Principal id="System">
      <UserId>S-1-5-18</UserId>
      <LogonType>ServiceAccount</LogonType>
      <RunLevel>HighestAvailable</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <StartWhenAvailable>true</StartWhenAvailable>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <RestartOnFailure>
      <Interval>PT30S</Interval>
      <Count>5</Count>
    </RestartOnFailure>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
  </Settings>
  <Actions Context="System">
    <Exec>
      <Command>powershell.exe</Command>
      <Arguments>-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "C:\ProgramData\MonitorAgent\run-agent.ps1"</Arguments>
    </Exec>
  </Actions>
</Task>
```

- [ ] **Step 4: Add the protected PowerShell launcher**

```powershell
param(
    [ValidateSet("run", "check-config", "health")]
    [string]$Command = "run"
)

$ErrorActionPreference = "Stop"
$InstallRoot = "C:\ProgramData\MonitorAgent"
$ConfigPath = Join-Path $InstallRoot "monitor-agent.env"
$AgentPath = Join-Path $InstallRoot "venv\Scripts\monitor-agent.exe"

if (-not (Test-Path -LiteralPath $ConfigPath -PathType Leaf)) {
    throw "Monitor Agent configuration is missing"
}

foreach ($Line in Get-Content -LiteralPath $ConfigPath -Encoding UTF8) {
    $Trimmed = $Line.Trim()
    if ($Trimmed.Length -eq 0 -or $Trimmed.StartsWith("#")) {
        continue
    }
    $Parts = $Trimmed.Split("=", 2)
    if ($Parts.Count -ne 2 -or $Parts[0] -notmatch "^[A-Z][A-Z0-9_]+$") {
        throw "Invalid Monitor Agent environment entry"
    }
    [Environment]::SetEnvironmentVariable($Parts[0], $Parts[1], "Process")
}

& $AgentPath $Command
exit $LASTEXITCODE
```

- [ ] **Step 5: Add install and uninstall scripts**

```powershell
# deploy/windows/install.ps1
param(
    [Parameter(Mandatory = $true)]
    [string]$WheelPath,
    [Parameter(Mandatory = $true)]
    [string]$EnvironmentFile
)

$ErrorActionPreference = "Stop"
$Principal = New-Object Security.Principal.WindowsPrincipal(
    [Security.Principal.WindowsIdentity]::GetCurrent()
)
if (-not $Principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "install.ps1 must run as Administrator"
}

$ScriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Split-Path -Parent (Split-Path -Parent $ScriptRoot)
$InstallRoot = "C:\ProgramData\MonitorAgent"
$Venv = Join-Path $InstallRoot "venv"
$LockPath = Join-Path $ProjectRoot "requirements.lock"

foreach ($Path in @($WheelPath, $EnvironmentFile, $LockPath)) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "Required installation file is missing"
    }
}

New-Item -ItemType Directory -Force -Path $InstallRoot | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $InstallRoot "logs") | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $InstallRoot "spool") | Out-Null

& py -3.14 -m venv $Venv
if ($LASTEXITCODE -ne 0) { throw "Python 3.14 virtual environment creation failed" }
& (Join-Path $Venv "Scripts\python.exe") -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) { throw "pip upgrade failed" }
& (Join-Path $Venv "Scripts\pip.exe") install --require-hashes -r $LockPath
if ($LASTEXITCODE -ne 0) { throw "locked dependency installation failed" }
& (Join-Path $Venv "Scripts\pip.exe") install --no-deps --force-reinstall $WheelPath
if ($LASTEXITCODE -ne 0) { throw "monitor-agent wheel installation failed" }

Copy-Item -Force $EnvironmentFile (Join-Path $InstallRoot "monitor-agent.env")
Copy-Item -Force (Join-Path $ScriptRoot "run-agent.ps1") $InstallRoot
Copy-Item -Force (Join-Path $ScriptRoot "monitor_agent_task.xml") $InstallRoot

$AclArguments = @(
    "/inheritance:r",
    "/grant:r",
    "*S-1-5-18:(OI)(CI)F",
    "*S-1-5-32-544:(OI)(CI)F"
)
& icacls $InstallRoot @AclArguments
if ($LASTEXITCODE -ne 0) { throw "Monitor Agent ACL configuration failed" }

& (Join-Path $InstallRoot "run-agent.ps1") -Command check-config
if ($LASTEXITCODE -ne 0) { throw "Monitor Agent configuration validation failed" }

& schtasks /Create /TN MonitorAgent /XML (Join-Path $InstallRoot "monitor_agent_task.xml") /F
if ($LASTEXITCODE -ne 0) { throw "Monitor Agent task registration failed" }
& schtasks /Run /TN MonitorAgent
if ($LASTEXITCODE -ne 0) { throw "Monitor Agent task start failed" }
```

```powershell
# deploy/windows/uninstall.ps1
param([switch]$Purge)

$ErrorActionPreference = "Stop"
$Principal = New-Object Security.Principal.WindowsPrincipal(
    [Security.Principal.WindowsIdentity]::GetCurrent()
)
if (-not $Principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "uninstall.ps1 must run as Administrator"
}

$InstallRoot = "C:\ProgramData\MonitorAgent"
& schtasks /End /TN MonitorAgent 2>$null
& schtasks /Delete /TN MonitorAgent /F 2>$null

if ($Purge) {
    Write-Host "Removing C:\ProgramData\MonitorAgent"
    Remove-Item -LiteralPath $InstallRoot -Recurse -Force -ErrorAction SilentlyContinue
    exit 0
}

foreach ($Name in @("venv", "run-agent.ps1", "monitor_agent_task.xml")) {
    Remove-Item -LiteralPath (Join-Path $InstallRoot $Name) -Recurse -Force -ErrorAction SilentlyContinue
}
Write-Host "Preserved Monitor Agent configuration, logs, and spool"
```

- [ ] **Step 6: Add Windows environment example**

Use the same keys as Linux, with:

```dotenv
MONITOR_SPOOL_PATH=C:\ProgramData\MonitorAgent\spool
MONITOR_LOG_PATH=C:\ProgramData\MonitorAgent\logs\monitor-agent.log
MONITOR_LOG_FORMAT=json
```

- [ ] **Step 7: Validate XML, PowerShell syntax, and tests**

Run on Windows:

```powershell
[xml](Get-Content deploy/windows/monitor_agent_task.xml -Raw) | Out-Null
$errors = $null
[System.Management.Automation.Language.Parser]::ParseFile(
    "deploy/windows/install.ps1",
    [ref]$null,
    [ref]$errors
) | Out-Null
if ($errors.Count -ne 0) { exit 1 }
```

Run: `python -m pytest tests/deploy/test_windows.py -q`

Expected: all checks pass.

- [ ] **Step 8: Commit**

```bash
git add deploy/windows tests/deploy/test_windows.py
git commit -m "deploy: validate Windows monitor agent task"
```

---

### Task 16: Managed macOS LaunchDaemon

**Files:**
- Create: `deploy/macos/com.company.monitor-agent.plist`
- Create: `deploy/macos/run-agent.sh`
- Create: `deploy/macos/install.sh`
- Create: `deploy/macos/uninstall.sh`
- Create: `deploy/macos/monitor-agent.env.example`
- Create: `tests/deploy/test_macos.py`

**Interfaces:**
- Consumes: built wheel, lock file, and environment file
- Produces: LaunchDaemon `com.company.monitor-agent`
- Produces: install root `/Library/Application Support/MonitorAgent`

- [ ] **Step 1: Write failing macOS deployment tests**

```python
import plistlib
from pathlib import Path


PLIST = Path("deploy/macos/com.company.monitor-agent.plist")


def test_launchdaemon_plist_is_valid_and_keeps_agent_alive() -> None:
    with PLIST.open("rb") as stream:
        data = plistlib.load(stream)
    assert data["Label"] == "com.company.monitor-agent"
    assert data["RunAtLoad"] is True
    assert data["KeepAlive"]["SuccessfulExit"] is False
    assert data["ProgramArguments"][-1].endswith("run-agent.sh")
    assert "MONITOR_API_TOKEN" not in repr(data)


def test_launcher_loads_protected_environment_and_execs_agent() -> None:
    text = Path("deploy/macos/run-agent.sh").read_text(encoding="utf-8")
    assert "set -a" in text
    assert "monitor-agent run" in text
    assert "exec " in text
```

- [ ] **Step 2: Run tests and verify macOS support is absent**

Run: `python -m pytest tests/deploy/test_macos.py -q`

Expected: files are missing.

- [ ] **Step 3: Add the LaunchDaemon plist**

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.company.monitor-agent</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/sh</string>
    <string>/Library/Application Support/MonitorAgent/run-agent.sh</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <dict>
    <key>SuccessfulExit</key>
    <false/>
  </dict>
  <key>ThrottleInterval</key>
  <integer>30</integer>
  <key>StandardOutPath</key>
  <string>/Library/Logs/MonitorAgent/launchd.stdout.log</string>
  <key>StandardErrorPath</key>
  <string>/Library/Logs/MonitorAgent/launchd.stderr.log</string>
  <key>ProcessType</key>
  <string>Background</string>
</dict>
</plist>
```

- [ ] **Step 4: Add the macOS launcher**

```bash
#!/bin/sh
set -eu

INSTALL_ROOT="/Library/Application Support/MonitorAgent"
CONFIG_FILE="$INSTALL_ROOT/monitor-agent.env"
AGENT="$INSTALL_ROOT/venv/bin/monitor-agent"
COMMAND=run

if [ "$#" -gt 1 ]; then
    echo "usage: run-agent.sh [run|check-config|health]" >&2
    exit 2
fi
if [ "$#" -eq 1 ]; then
    COMMAND=$1
fi
case "$COMMAND" in
    run|check-config|health) ;;
    *)
        echo "unsupported Monitor Agent command" >&2
        exit 2
        ;;
esac

if [ ! -r "$CONFIG_FILE" ]; then
    echo "Monitor Agent configuration is missing" >&2
    exit 2
fi

set -a
. "$CONFIG_FILE"
set +a
exec "$AGENT" "$COMMAND"
```

- [ ] **Step 5: Add idempotent macOS install and uninstall**

```bash
#!/bin/sh
# deploy/macos/install.sh
set -eu

if [ "$(id -u)" -ne 0 ]; then
    echo "install.sh must run as root" >&2
    exit 2
fi
if [ "$#" -ne 2 ]; then
    echo "usage: install.sh WHEEL_PATH ENV_FILE" >&2
    exit 2
fi

WHEEL_PATH=$1
ENV_SOURCE=$2
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PROJECT_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/../.." && pwd)
INSTALL_ROOT="/Library/Application Support/MonitorAgent"
LOG_ROOT="/Library/Logs/MonitorAgent"
STATE_ROOT="/Library/Application Support/MonitorAgent/spool"
PLIST_TARGET="/Library/LaunchDaemons/com.company.monitor-agent.plist"

test -f "$WHEEL_PATH"
test -f "$ENV_SOURCE"
test -f "$PROJECT_DIR/requirements.lock"

install -d -m 0755 "$INSTALL_ROOT"
install -d -m 0700 "$LOG_ROOT" "$STATE_ROOT"
python3.14 -m venv "$INSTALL_ROOT/venv"
"$INSTALL_ROOT/venv/bin/python" -m pip install --upgrade pip
"$INSTALL_ROOT/venv/bin/pip" install --require-hashes -r "$PROJECT_DIR/requirements.lock"
"$INSTALL_ROOT/venv/bin/pip" install --no-deps --force-reinstall "$WHEEL_PATH"
install -m 0700 "$SCRIPT_DIR/run-agent.sh" "$INSTALL_ROOT/run-agent.sh"
install -m 0600 "$ENV_SOURCE" "$INSTALL_ROOT/monitor-agent.env"
install -m 0644 "$SCRIPT_DIR/com.company.monitor-agent.plist" "$PLIST_TARGET"
chown -R root:wheel "$INSTALL_ROOT" "$LOG_ROOT" "$PLIST_TARGET"

"$INSTALL_ROOT/run-agent.sh" check-config
plutil -lint "$PLIST_TARGET"
launchctl bootout system/com.company.monitor-agent 2>/dev/null || true
launchctl bootstrap system "$PLIST_TARGET"
launchctl enable system/com.company.monitor-agent
```

```bash
#!/bin/sh
# deploy/macos/uninstall.sh
set -eu

if [ "$(id -u)" -ne 0 ]; then
    echo "uninstall.sh must run as root" >&2
    exit 2
fi

PURGE=false
if [ "$#" -gt 1 ]; then
    echo "usage: uninstall.sh [--purge]" >&2
    exit 2
fi
if [ "$#" -eq 1 ]; then
    if [ "$1" != "--purge" ]; then
        echo "usage: uninstall.sh [--purge]" >&2
        exit 2
    fi
    PURGE=true
fi

INSTALL_ROOT="/Library/Application Support/MonitorAgent"
launchctl bootout system/com.company.monitor-agent 2>/dev/null || true
rm -f /Library/LaunchDaemons/com.company.monitor-agent.plist

if [ "$PURGE" = true ]; then
    echo "Removing Monitor Agent configuration, logs, and spool"
    rm -rf "$INSTALL_ROOT" "/Library/Logs/MonitorAgent"
else
    rm -rf "$INSTALL_ROOT/venv"
    rm -f "$INSTALL_ROOT/run-agent.sh"
    echo "Preserved Monitor Agent configuration, logs, and spool"
fi
```

Add `monitor-agent.env.example`:

```dotenv
MONITOR_COLLECTOR_URI=https://collector.internal/api/v1/telemetry
MONITOR_API_TOKEN=replace-with-managed-secret
MONITOR_HEARTBEAT_SEC=300
MONITOR_STARTUP_DELAY_SEC=30
MONITOR_SPOOL_PATH="/Library/Application Support/MonitorAgent/spool"
MONITOR_LOG_PATH="/Library/Logs/MonitorAgent/monitor-agent.log"
MONITOR_PROCESS_CMDLINE_MODE=redacted
MONITOR_LOG_FORMAT=json
```

- [ ] **Step 6: Validate shell, plist, and tests**

Run on macOS: `plutil -lint deploy/macos/com.company.monitor-agent.plist`

Run: `sh -n deploy/macos/run-agent.sh deploy/macos/install.sh deploy/macos/uninstall.sh`

Run: `python -m pytest tests/deploy/test_macos.py -q`

Expected: all commands exit 0.

- [ ] **Step 7: Commit**

```bash
git add deploy/macos tests/deploy/test_macos.py
git commit -m "deploy: add macOS monitor agent launchdaemon"
```

---

### Task 17: Cross-Platform CI and Supply-Chain Gates

**Files:**
- Create: `.github/workflows/ci.yml`
- Modify: `pyproject.toml`

**Interfaces:**
- Consumes: package, tests, deployment files, and `requirements.lock`
- Produces: required lint, typing, test, package, deployment, and audit jobs

- [ ] **Step 1: Confirm the repository has no automated gate**

Run: `test ! -f .github/workflows/ci.yml`

Expected: exit 0 before the workflow is added.

- [ ] **Step 2: Add the CI workflow**

```yaml
name: ci

on:
  push:
  pull_request:

permissions:
  contents: read

jobs:
  quality:
    runs-on: ubuntu-24.04
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.14"
          cache: pip
      - run: python -m pip install -e ".[dev]"
      - run: ruff format --check .
      - run: ruff check .
      - run: mypy src/monitor_agent
      - run: python -m pip install --dry-run --require-hashes -r requirements.lock
      - run: pip-audit -r requirements.lock --disable-pip

  test:
    strategy:
      fail-fast: false
      matrix:
        os: [ubuntu-24.04, windows-2025, macos-15]
        python: ["3.11", "3.12", "3.13", "3.14"]
    runs-on: ${{ matrix.os }}
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: ${{ matrix.python }}
          cache: pip
      - run: python -m pip install -e ".[dev]"
      - run: python -m pytest --cov=monitor_agent --cov-branch --cov-report=term-missing

  package:
    runs-on: ubuntu-24.04
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.14"
      - run: python -m pip install build twine
      - run: python -m build
      - run: python -m twine check dist/*
      - uses: actions/upload-artifact@v4
        with:
          name: monitor-agent-2.0.0
          path: dist/
          if-no-files-found: error

  deployment:
    runs-on: ubuntu-24.04
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.14"
      - run: python -m pip install pytest
      - run: python -m pytest tests/deploy -q
      - run: bash -n deploy/linux/install.sh deploy/linux/uninstall.sh
      - run: systemd-analyze verify deploy/linux/monitor-agent.service
```

Set `timeout-minutes: 15` on every job. Do not grant write permissions or use pull-request secrets.

- [ ] **Step 3: Make coverage enforcement explicit in local and CI runs**

Update `pyproject.toml`:

```toml
[tool.pytest.ini_options]
addopts = "--strict-config --strict-markers --cov=monitor_agent --cov-branch --cov-report=term-missing"
testpaths = ["tests"]

[tool.coverage.report]
fail_under = 90
show_missing = true
skip_covered = true
```

- [ ] **Step 4: Run the complete quality sequence locally**

Run: `ruff format --check .`

Run: `ruff check .`

Run: `mypy src/monitor_agent`

Run: `python -m pytest`

Run: `python -m build`

Run: `python -m twine check dist/*`

Run: `pip-audit -r requirements.lock --disable-pip`

Expected: every command exits 0; coverage reports at least 90% for lines and branches.

- [ ] **Step 5: Commit**

```bash
git add .github/workflows/ci.yml pyproject.toml
git commit -m "ci: gate monitor agent release quality"
```

---

### Task 18: Operator Materials, Migration, and Final Acceptance

**Files:**
- Create: `README.md`
- Create: `CHANGELOG.md`
- Create: `SECURITY.md`
- Create: `PRIVACY.md`
- Create: `docs/migration-v1-to-v2.md`
- Create: `docs/operations.md`
- Modify: `docs/superpowers/specs/2026-07-20-monitor-agent-v2-design.md` only if implementation revealed an approved factual correction

**Interfaces:**
- Consumes: completed CLI, configuration, spool, deployment, and release artifacts
- Produces: exact install, operate, migrate, recover, and rollback procedures

- [ ] **Step 1: Write README verification test**

```python
from pathlib import Path


def test_readme_names_every_supported_platform_and_command() -> None:
    text = Path("README.md").read_text(encoding="utf-8")
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
    ):
        assert value in text


def test_privacy_file_names_sensitive_controls() -> None:
    text = Path("PRIVACY.md").read_text(encoding="utf-8")
    assert "MONITOR_PROCESS_CMDLINE_MODE" in text
    assert "redacted" in text
    assert "screenshots" in text
    assert "keystrokes" in text
```

Save as `tests/test_operator_materials.py` and run it before creating the files.

Run: `python -m pytest tests/test_operator_materials.py -q`

Expected: failures identify missing operator files.

- [ ] **Step 2: Write `README.md` with exact operator flow**

The README contains these sections in order:

1. What the agent collects and explicitly does not collect.
2. Supported Python and operating-system versions.
3. Build commands: create venv, install `.[dev]`, run quality gates, build wheel.
4. Required configuration with an environment-variable table containing defaults, ranges, and sensitivity.
5. CLI command examples and exit-code table.
6. Linux, Windows, and macOS installation commands using the scripts from Tasks 14-16.
7. Upgrade and rollback links.
8. Telemetry schema compatibility statement.
9. Security and privacy links.

The quick verification block is exact:

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install -e ".[dev]"
monitor-agent version
monitor-agent once --event heartbeat --no-transmit
python -m pytest
```

- [ ] **Step 3: Write release, security, and privacy files**

`CHANGELOG.md` starts with:

```markdown
# Changelog

## 2.0.0 - 2026-07-20

### Added
- Installable cross-platform package and operational CLI.
- Failure-isolated collectors with structured status metadata.
- Classified HTTP retries and bounded atomic offline spool.
- Hardened Linux, Windows, and macOS deployment workflows.
- Cross-platform tests, typing, linting, package, and dependency gates.

### Changed
- Production runtime target moves to Python 3.14.6.
- Machine identity now derives from hashed platform identifiers.
- Process command lines default to secret redaction.
- Telemetry scheduling uses a monotonic standard-library loop.

### Removed
- In-process `schedule` dependency.
- Inline service credentials and hard-coded Python 3.11 paths.
```

`SECURITY.md` states HTTPS-only transport, custom CA handling, owner-only configuration/spool permissions, root/SYSTEM privilege rationale, secret-safe logging, dependency audit commands, and this exact reporting path: use the repository Security tab's private vulnerability report; never open a public issue for an undisclosed vulnerability.

`PRIVACY.md` provides a table for every payload section, its fields, purpose, default state, and control. It states that screenshots, keystrokes, file contents, browser content, and employee scoring are not collected. It explains `none/redacted/full` command-line modes and warns that `full` can transmit secrets supplied by other processes.

- [ ] **Step 4: Write migration and operations procedures**

`docs/migration-v1-to-v2.md` contains:

- Preflight backup of the existing service/task and environment.
- Build and checksum of the 2.0.0 wheel.
- Side-by-side virtual environment installation.
- `check-config` and `once --no-transmit` gates.
- Service switch and first-heartbeat verification.
- Spool verification.
- Platform-specific rollback commands restoring the previous executable path.
- Explicit statement that rollback does not delete the v2 spool.

`docs/operations.md` contains:

- Health command interpretation.
- Log locations on all three platforms.
- Expected startup and heartbeat log events.
- HTTP class response table matching `DeliveryKind`.
- Authentication recovery that keeps records queued.
- Spool size/age eviction behavior.
- Dead-letter inspection without printing payload contents.
- CA rotation, API-token rotation, and agent upgrade procedures.
- Troubleshooting for access-denied collectors, package-manager timeout, invalid config, full spool, and clock changes.

- [ ] **Step 5: Run operator-file tests**

Run: `python -m pytest tests/test_operator_materials.py -q`

Expected: all tests pass.

- [ ] **Step 6: Execute the full acceptance matrix**

Run:

```bash
ruff format --check .
ruff check .
mypy src/monitor_agent
python -m pytest --cov=monitor_agent --cov-branch --cov-report=term-missing
python -m build
python -m twine check dist/*
python -m pip install --dry-run --require-hashes -r requirements.lock
pip-audit -r requirements.lock --disable-pip
systemd-analyze verify deploy/linux/monitor-agent.service
bash -n deploy/linux/install.sh deploy/linux/uninstall.sh
sh -n deploy/macos/run-agent.sh deploy/macos/install.sh deploy/macos/uninstall.sh
```

Expected:

- Every command exits 0.
- Coverage is at least 90% for lines and branches.
- The built wheel and source distribution both report version 2.0.0.
- Dependency audit reports no known vulnerabilities.
- Deployment definitions parse on their target platforms.

- [ ] **Step 7: Run a local no-transmit smoke test**

Run:

```bash
MONITOR_SPOOL_PATH=/tmp/monitor-agent-smoke-spool \
MONITOR_STARTUP_DELAY_SEC=0 \
monitor-agent check-config

MONITOR_SPOOL_PATH=/tmp/monitor-agent-smoke-spool \
monitor-agent once --event heartbeat --no-transmit
```

Expected:

- `check-config` prints `configuration valid`.
- `once` prints one JSON object with `schema_version: 1.0`, an event UUID, every legacy section, and agent version 2.0.0.
- Output contains no authorization token or raw platform machine identifier.

- [ ] **Step 8: Verify migration safety from repository state**

Run: `git diff --check`

Run: `git status --short`

Expected: no unintended files, secrets, runtime logs, spool records, virtual environments, or build products are tracked.

Run: `git grep -n -E 'MONITOR_API_TOKEN=.+|Authorization: Bearer .+' -- ':!*.example' ':!tests/**'`

Expected: no live credential values.

- [ ] **Step 9: Commit operator materials**

```bash
git add README.md CHANGELOG.md SECURITY.md PRIVACY.md docs/migration-v1-to-v2.md docs/operations.md tests/test_operator_materials.py
git commit -m "docs: ship monitor agent operations package"
```

- [ ] **Step 10: Create the release checkpoint**

Run: `git log --oneline --decorate -20`

Expected: eighteen focused task commits plus the design and plan commits, with no fixup commits and no uncommitted implementation files.

Tag only after the user authorizes release publication:

```bash
git tag -a v2.0.0 -m "monitor-agent 2.0.0"
```
