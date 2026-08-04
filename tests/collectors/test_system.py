from __future__ import annotations

import json
from types import SimpleNamespace

from monitor_agent.collectors.system import SystemCollector
from monitor_agent.identity import MachineIdentity
from monitor_agent.models import CollectorStatus


def test_system_collector_preserves_schema_with_private_stable_identity(monkeypatch) -> None:
    boot_calls = 0

    def boot_time() -> float:
        nonlocal boot_calls
        boot_calls += 1
        return 1_700_000_000.0

    monkeypatch.setattr("monitor_agent.collectors.system.socket.getfqdn", lambda: "host.example")
    monkeypatch.setattr(
        "monitor_agent.collectors.system.platform.uname",
        lambda: SimpleNamespace(
            system="TestOS",
            release="1.2",
            version="build-3",
            machine="test64",
            processor="test-cpu",
        ),
    )
    monkeypatch.setattr("monitor_agent.collectors.system.psutil.boot_time", boot_time)
    monkeypatch.setattr("monitor_agent.collectors.system.time.time", lambda: 1_700_000_123.9)
    monkeypatch.setattr("monitor_agent.collectors.system.sys.version", "3.14.6 test-runtime")

    collector = SystemCollector(MachineIdentity("hashed-id", "raw-source-must-not-leak"))
    result = collector.collect()

    assert collector.name == "system"
    assert result.status is CollectorStatus.SUCCESS
    assert result.data == {
        "system": {
            "hostname": "host.example",
            "machine_id": "hashed-id",
            "os": "TestOS",
            "os_release": "1.2",
            "os_version": "build-3",
            "architecture": "test64",
            "processor": "test-cpu",
            "python": "3.14.6 test-runtime",
            "boot_time": "2023-11-14T22:13:20+00:00",
            "uptime_sec": 123,
        }
    }
    assert boot_calls == 1
    assert "raw-source-must-not-leak" not in repr(result)
    json.dumps(result.data, allow_nan=False)


def test_system_collector_clamps_negative_uptime(monkeypatch) -> None:
    monkeypatch.setattr("monitor_agent.collectors.system.psutil.boot_time", lambda: 200.0)
    monkeypatch.setattr("monitor_agent.collectors.system.time.time", lambda: 100.0)

    system = SystemCollector(MachineIdentity("hashed-id", "test")).collect().data["system"]

    assert system["uptime_sec"] == 0
    assert system["boot_time"] == "1970-01-01T00:03:20+00:00"
