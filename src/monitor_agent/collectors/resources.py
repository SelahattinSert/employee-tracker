from __future__ import annotations

import math
from collections.abc import Callable
from typing import Protocol, cast

import psutil  # type: ignore[import-untyped]

from monitor_agent.models import CollectorPayload, CollectorStatus, JSONValue


class _CpuFrequency(Protocol):
    current: float


class _VirtualMemory(Protocol):
    total: int
    available: int
    used: int
    percent: float


class _SwapMemory(Protocol):
    total: int
    used: int
    percent: float


class _DiskPartition(Protocol):
    device: str
    mountpoint: str
    fstype: str


class _DiskUsage(Protocol):
    total: int
    used: int
    free: int
    percent: float


class ResourceCollector:
    name = "resources"

    def __init__(self, cpu_sample_sec: float = 1.0) -> None:
        if not math.isfinite(cpu_sample_sec) or cpu_sample_sec < 0:
            raise ValueError("cpu_sample_sec must be finite and non-negative")
        self._cpu_sample_sec = float(cpu_sample_sec)

    def collect(self) -> CollectorPayload:
        cpu = self._collect_cpu()
        memory = self._collect_memory()
        disks, disk_access_partial = self._collect_disks()
        data: dict[str, JSONValue] = {
            "cpu": cpu,
            "memory": memory,
            "disks": disks,
        }
        if disk_access_partial:
            return CollectorPayload(
                data=data,
                status=CollectorStatus.PARTIAL,
                error_code="disk_access_partial",
                error_message="some disks unavailable",
            )
        return CollectorPayload(data=data)

    def _collect_cpu(self) -> dict[str, JSONValue]:
        physical_count = cast(int | None, psutil.cpu_count(logical=False))
        logical_count = cast(int | None, psutil.cpu_count(logical=True))
        raw_percentages = cast(
            list[float],
            psutil.cpu_percent(interval=self._cpu_sample_sec, percpu=True),
        )
        percentages = [float(value) for value in raw_percentages]
        percentage_values: list[JSONValue] = [value for value in percentages]
        percent_total = (
            round(sum(percentages) / len(percentages), 2) if percentages else 0.0
        )
        frequency = cast(_CpuFrequency | None, psutil.cpu_freq())

        load_average: JSONValue = None
        if hasattr(psutil, "getloadavg"):
            get_load_average = cast(
                Callable[[], tuple[float, float, float]],
                psutil.getloadavg,
            )
            load_average = [float(value) for value in get_load_average()]

        return {
            "physical_cores": max(0, physical_count or 0),
            "logical_cores": max(0, logical_count or len(percentages)),
            "percent_total": percent_total,
            "percent_per": percentage_values,
            "freq_mhz": float(frequency.current) if frequency is not None else None,
            "load_avg": load_average,
        }

    @staticmethod
    def _collect_memory() -> dict[str, JSONValue]:
        virtual_memory = cast(_VirtualMemory, psutil.virtual_memory())
        swap_memory = cast(_SwapMemory, psutil.swap_memory())
        ram: dict[str, JSONValue] = {
            "total_kb": int(virtual_memory.total) // 1024,
            "available_kb": int(virtual_memory.available) // 1024,
            "used_kb": int(virtual_memory.used) // 1024,
            "percent": float(virtual_memory.percent),
        }
        swap: dict[str, JSONValue] = {
            "total_kb": int(swap_memory.total) // 1024,
            "used_kb": int(swap_memory.used) // 1024,
            "percent": float(swap_memory.percent),
        }
        return {"ram": ram, "swap": swap}

    @staticmethod
    def _collect_disks() -> tuple[list[JSONValue], bool]:
        partitions = cast(list[_DiskPartition], psutil.disk_partitions(all=False))
        disks: list[JSONValue] = []
        disk_access_partial = False
        for partition in partitions:
            try:
                usage = cast(_DiskUsage, psutil.disk_usage(partition.mountpoint))
            except OSError:
                disk_access_partial = True
                continue
            disks.append(
                {
                    "device": partition.device,
                    "mountpoint": partition.mountpoint,
                    "fstype": partition.fstype,
                    "total_gb": round(int(usage.total) / 1024**3, 2),
                    "used_gb": round(int(usage.used) / 1024**3, 2),
                    "free_gb": round(int(usage.free) / 1024**3, 2),
                    "percent": float(usage.percent),
                }
            )
        return disks, disk_access_partial
