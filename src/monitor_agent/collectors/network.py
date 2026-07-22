from __future__ import annotations

import math
import socket
from collections.abc import Iterable, Mapping, Sequence
from typing import Protocol, cast

import psutil  # type: ignore[import-untyped]

from monitor_agent.models import CollectorPayload, CollectorStatus, JSONValue


class _InterfaceAddress(Protocol):
    family: object
    address: object


class _InterfaceStats(Protocol):
    isup: object
    speed: object
    mtu: object


class _IoCounters(Protocol):
    bytes_sent: object
    bytes_recv: object
    packets_sent: object
    packets_recv: object
    errin: object
    errout: object


class _Connection(Protocol):
    fd: object
    family: object
    type: object
    laddr: object
    raddr: object
    status: object
    pid: object


class _SocketAddress(Protocol):
    ip: object
    port: object


def _optional_string(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _optional_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _optional_number(value: object) -> int | float | None:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    return None


def _enum_name(value: object) -> str:
    try:
        name = getattr(value, "name", None)
    except Exception:
        return "UNKNOWN"
    if isinstance(name, str):
        return name
    if value is None or isinstance(value, (str, int)):
        return str(value)
    return "UNKNOWN"


def _format_address(address: object) -> str | None:
    if address is None:
        return None
    try:
        if isinstance(address, Sequence) and not isinstance(address, (str, bytes, bytearray)):
            if len(address) < 2:
                return None
            host = address[0]
            port = address[1]
        else:
            named_address = cast(_SocketAddress, address)
            host = named_address.ip
            port = named_address.port
    except (AttributeError, IndexError, TypeError):
        return None
    if not isinstance(host, str):
        return None
    if not isinstance(port, int) or isinstance(port, bool):
        return None
    return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"


def _connection_record(connection: _Connection) -> dict[str, JSONValue] | None:
    try:
        return {
            "fd": _optional_int(connection.fd),
            "family": _enum_name(connection.family),
            "type": _enum_name(connection.type),
            "laddr": _format_address(connection.laddr),
            "raddr": _format_address(connection.raddr),
            "status": _optional_string(connection.status),
            "pid": _optional_int(connection.pid),
        }
    except Exception:
        return None


def _connection_sort_key(record: Mapping[str, JSONValue]) -> tuple[object, ...]:
    return (
        cast(str, record["family"]).casefold(),
        cast(str, record["type"]).casefold(),
        cast(str | None, record["laddr"]) or "",
        cast(str | None, record["raddr"]) or "",
        cast(str | None, record["status"]) or "",
        cast(int | None, record["pid"]) if record["pid"] is not None else -1,
        cast(int | None, record["fd"]) if record["fd"] is not None else -1,
    )


def _collect_adapters() -> list[JSONValue]:
    addresses = cast(
        Mapping[str, Iterable[_InterfaceAddress]],
        psutil.net_if_addrs(),
    )
    stats = cast(Mapping[str, _InterfaceStats], psutil.net_if_stats())
    link_family = getattr(psutil, "AF_LINK", object())
    adapters: list[dict[str, JSONValue]] = []
    for interface, address_values in addresses.items():
        interface_stats = stats.get(interface)
        if interface_stats is None or interface_stats.isup is not True:
            continue
        ipv4: str | None = None
        mac: str | None = None
        for address in address_values:
            if address.family == socket.AF_INET and ipv4 is None:
                ipv4 = _optional_string(address.address)
            if address.family == link_family and mac is None:
                mac = _optional_string(address.address)
        adapters.append(
            {
                "interface": interface,
                "ipv4": ipv4,
                "mac": mac,
                "speed_mb": _optional_number(interface_stats.speed),
                "mtu": _optional_number(interface_stats.mtu),
            }
        )
    adapters.sort(key=lambda record: cast(str, record["interface"]).casefold())
    return [record for record in adapters]


def _collect_io() -> dict[str, JSONValue]:
    counters = cast(_IoCounters | None, psutil.net_io_counters())
    if counters is None:
        return {}
    return {
        "bytes_sent": _optional_number(counters.bytes_sent),
        "bytes_recv": _optional_number(counters.bytes_recv),
        "packets_sent": _optional_number(counters.packets_sent),
        "packets_recv": _optional_number(counters.packets_recv),
        "errin": _optional_number(counters.errin),
        "errout": _optional_number(counters.errout),
    }


class NetworkCollector:
    name = "network"

    def __init__(self, enabled: bool = True) -> None:
        self._enabled = enabled

    def collect(self) -> CollectorPayload:
        if not self._enabled:
            return CollectorPayload(
                data={"adapters": [], "connections": [], "io": {}},
                status=CollectorStatus.DISABLED,
            )

        adapters = _collect_adapters()
        io = _collect_io()
        try:
            raw_connections = cast(
                Iterable[_Connection],
                psutil.net_connections(kind="inet"),
            )
            connection_records = [
                record
                for connection in raw_connections
                if (record := _connection_record(connection)) is not None
            ]
            connection_records.sort(key=_connection_sort_key)
            connections: list[JSONValue] = [record for record in connection_records]
        except (PermissionError, psutil.AccessDenied):
            return CollectorPayload(
                data={"adapters": adapters, "connections": [], "io": io},
                status=CollectorStatus.PARTIAL,
                error_code="network_connections_denied",
                error_message="network connections unavailable",
            )

        return CollectorPayload(data={"adapters": adapters, "connections": connections, "io": io})
