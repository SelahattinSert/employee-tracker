from __future__ import annotations

import json
from types import SimpleNamespace

import psutil
import pytest

from monitor_agent.collectors.resources import ResourceCollector
from monitor_agent.models import CollectorStatus

GIB = 1024**3


def install_memory_fakes(monkeypatch) -> None:
    monkeypatch.setattr(
        "monitor_agent.collectors.resources.psutil.virtual_memory",
        lambda: SimpleNamespace(total=4097, available=3073, used=1025, percent=25.125),
    )
    monkeypatch.setattr(
        "monitor_agent.collectors.resources.psutil.swap_memory",
        lambda: SimpleNamespace(total=2049, used=1025, percent=50.25),
    )


def install_empty_disk_fake(monkeypatch) -> None:
    monkeypatch.setattr(
        "monitor_agent.collectors.resources.psutil.disk_partitions",
        lambda all=False: [],
    )


def test_resource_collector_preserves_schema_units_rounding_and_one_cpu_sample(
    monkeypatch,
) -> None:
    count_calls: list[bool] = []
    percent_calls: list[tuple[float, bool]] = []

    def cpu_count(*, logical: bool) -> int:
        count_calls.append(logical)
        return 8 if logical else 4

    def cpu_percent(*, interval: float, percpu: bool) -> list[float]:
        percent_calls.append((interval, percpu))
        return [10.125, 20.126]

    monkeypatch.setattr("monitor_agent.collectors.resources.psutil.cpu_count", cpu_count)
    monkeypatch.setattr("monitor_agent.collectors.resources.psutil.cpu_percent", cpu_percent)
    monkeypatch.setattr(
        "monitor_agent.collectors.resources.psutil.cpu_freq",
        lambda: SimpleNamespace(current=2400.25),
    )
    monkeypatch.setattr(
        "monitor_agent.collectors.resources.psutil.getloadavg",
        lambda: (1.25, 2.5, 3.75),
    )
    install_memory_fakes(monkeypatch)
    monkeypatch.setattr(
        "monitor_agent.collectors.resources.psutil.disk_partitions",
        lambda all=False: [SimpleNamespace(device="/dev/root", mountpoint="/", fstype="ext4")],
    )
    monkeypatch.setattr(
        "monitor_agent.collectors.resources.psutil.disk_usage",
        lambda path: SimpleNamespace(
            total=int(10.126 * GIB),
            used=int(4.444 * GIB),
            free=int(5.682 * GIB),
            percent=43.89,
        ),
    )

    collector = ResourceCollector(cpu_sample_sec=0.25)
    result = collector.collect()

    assert collector.name == "resources"
    assert result.status is CollectorStatus.SUCCESS
    assert result.data == {
        "cpu": {
            "physical_cores": 4,
            "logical_cores": 8,
            "percent_total": 15.13,
            "percent_per": [10.125, 20.126],
            "freq_mhz": 2400.25,
            "load_avg": [1.25, 2.5, 3.75],
        },
        "memory": {
            "ram": {
                "total_kb": 4,
                "available_kb": 3,
                "used_kb": 1,
                "percent": 25.125,
            },
            "swap": {"total_kb": 2, "used_kb": 1, "percent": 50.25},
        },
        "disks": [
            {
                "device": "/dev/root",
                "mountpoint": "/",
                "fstype": "ext4",
                "total_gb": 10.13,
                "used_gb": 4.44,
                "free_gb": 5.68,
                "percent": 43.89,
            }
        ],
    }
    assert count_calls == [False, True]
    assert percent_calls == [(0.25, True)]
    json.dumps(result.data, allow_nan=False)


def test_resource_collector_handles_empty_cpu_samples_and_optional_metrics(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "monitor_agent.collectors.resources.psutil.cpu_count",
        lambda logical=True: 0 if logical else None,
    )
    monkeypatch.setattr(
        "monitor_agent.collectors.resources.psutil.cpu_percent",
        lambda interval, percpu: [],
    )
    monkeypatch.setattr("monitor_agent.collectors.resources.psutil.cpu_freq", lambda: None)
    monkeypatch.delattr(psutil, "getloadavg", raising=False)
    install_memory_fakes(monkeypatch)
    install_empty_disk_fake(monkeypatch)

    result = ResourceCollector(cpu_sample_sec=0).collect()

    assert result.status is CollectorStatus.SUCCESS
    assert result.data["cpu"] == {
        "physical_cores": 0,
        "logical_cores": 0,
        "percent_total": 0.0,
        "percent_per": [],
        "freq_mhz": None,
        "load_avg": None,
    }
    assert result.data["disks"] == []


@pytest.mark.parametrize("unavailable_count", [None, 0])
def test_resource_collector_uses_sample_count_when_logical_count_is_unavailable(
    monkeypatch,
    unavailable_count: int | None,
) -> None:
    monkeypatch.setattr(
        "monitor_agent.collectors.resources.psutil.cpu_count",
        lambda logical=True: unavailable_count,
    )
    monkeypatch.setattr(
        "monitor_agent.collectors.resources.psutil.cpu_percent",
        lambda interval, percpu: [1, 2, 3],
    )
    monkeypatch.setattr("monitor_agent.collectors.resources.psutil.cpu_freq", lambda: None)
    monkeypatch.delattr(psutil, "getloadavg", raising=False)
    install_memory_fakes(monkeypatch)
    install_empty_disk_fake(monkeypatch)

    cpu = ResourceCollector(cpu_sample_sec=0).collect().data["cpu"]

    assert cpu["physical_cores"] == 0
    assert cpu["logical_cores"] == 3
    assert cpu["percent_total"] == 2.0
    assert cpu["percent_per"] == [1.0, 2.0, 3.0]


def test_resource_collector_isolates_each_inaccessible_disk_without_leaking(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "monitor_agent.collectors.resources.psutil.cpu_count",
        lambda logical=True: 1,
    )
    monkeypatch.setattr(
        "monitor_agent.collectors.resources.psutil.cpu_percent",
        lambda interval, percpu: [0.0],
    )
    monkeypatch.setattr("monitor_agent.collectors.resources.psutil.cpu_freq", lambda: None)
    monkeypatch.delattr(psutil, "getloadavg", raising=False)
    install_memory_fakes(monkeypatch)
    partitions = [
        SimpleNamespace(device="/dev/a", mountpoint="/shared", fstype="ext4"),
        SimpleNamespace(
            device="secret-permission-device",
            mountpoint="/secret/permission",
            fstype="secretfs",
        ),
        SimpleNamespace(
            device="secret-os-device",
            mountpoint="/secret/os-error",
            fstype="secretfs",
        ),
        SimpleNamespace(device="/dev/b", mountpoint="/shared", fstype="xfs"),
    ]
    disk_calls: list[str] = []

    def disk_usage(path: str) -> SimpleNamespace:
        disk_calls.append(path)
        if path == "/secret/permission":
            raise PermissionError("private permission detail token=secret")
        if path == "/secret/os-error":
            raise OSError("private operating-system detail token=secret")
        return SimpleNamespace(total=GIB, used=GIB // 2, free=GIB // 2, percent=50)

    monkeypatch.setattr(
        "monitor_agent.collectors.resources.psutil.disk_partitions",
        lambda all=False: partitions,
    )
    monkeypatch.setattr("monitor_agent.collectors.resources.psutil.disk_usage", disk_usage)

    result = ResourceCollector(cpu_sample_sec=0).collect()

    assert result.status is CollectorStatus.PARTIAL
    assert result.error_code == "disk_access_partial"
    assert result.error_message == "some disks unavailable"
    assert [disk["device"] for disk in result.data["disks"]] == ["/dev/a", "/dev/b"]
    assert disk_calls == [
        "/shared",
        "/secret/permission",
        "/secret/os-error",
        "/shared",
    ]
    assert "secret" not in repr(result)
    assert "private" not in repr(result)


@pytest.mark.parametrize("cpu_sample_sec", [-0.1, float("inf"), float("nan")])
def test_resource_collector_rejects_invalid_cpu_sample_intervals(cpu_sample_sec: float) -> None:
    with pytest.raises(ValueError, match="finite and non-negative"):
        ResourceCollector(cpu_sample_sec=cpu_sample_sec)
