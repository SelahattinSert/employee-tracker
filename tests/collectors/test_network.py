from __future__ import annotations

import json
import socket
from types import SimpleNamespace

import psutil
import pytest

from monitor_agent.collectors.network import NetworkCollector
from monitor_agent.models import CollectorStatus


def install_network_fakes(
    monkeypatch: pytest.MonkeyPatch,
    connection_result: object,
) -> None:
    monkeypatch.setattr(
        "monitor_agent.collectors.network.psutil.net_if_addrs",
        lambda: {
            "eth0": [
                SimpleNamespace(family=socket.AF_INET, address="10.0.0.2"),
                SimpleNamespace(
                    family=getattr(psutil, "AF_LINK", object()),
                    address="00:11:22:33:44:55",
                ),
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


def test_network_preserves_schema_formats_addresses_and_sorts(monkeypatch) -> None:
    ipv6_connection = SimpleNamespace(
        fd=7,
        family=socket.AddressFamily.AF_INET6,
        type=socket.SocketKind.SOCK_STREAM,
        laddr=SimpleNamespace(ip="::1", port=443),
        raddr=SimpleNamespace(ip="2001:db8::1", port=54000),
        status="ESTABLISHED",
        pid=44,
    )
    ipv4_connection = SimpleNamespace(
        fd=3,
        family=socket.AddressFamily.AF_INET,
        type=socket.SocketKind.SOCK_DGRAM,
        laddr=("127.0.0.1", 53),
        raddr=(),
        status="NONE",
        pid=None,
    )
    install_network_fakes(
        monkeypatch,
        lambda kind: [ipv6_connection, ipv4_connection],
    )

    collector = NetworkCollector()
    result = collector.collect()

    assert collector.name == "network"
    assert result.status is CollectorStatus.SUCCESS
    assert result.data == {
        "adapters": [
            {
                "interface": "eth0",
                "ipv4": "10.0.0.2",
                "mac": "00:11:22:33:44:55",
                "speed_mb": 1000,
                "mtu": 1500,
            }
        ],
        "connections": [
            {
                "fd": 3,
                "family": "AF_INET",
                "type": "SOCK_DGRAM",
                "laddr": "127.0.0.1:53",
                "raddr": None,
                "status": "NONE",
                "pid": None,
            },
            {
                "fd": 7,
                "family": "AF_INET6",
                "type": "SOCK_STREAM",
                "laddr": "[::1]:443",
                "raddr": "[2001:db8::1]:54000",
                "status": "ESTABLISHED",
                "pid": 44,
            },
        ],
        "io": {
            "bytes_sent": 1,
            "bytes_recv": 2,
            "packets_sent": 3,
            "packets_recv": 4,
            "errin": 0,
            "errout": 0,
        },
    }
    json.dumps(result.data, allow_nan=False)


@pytest.mark.parametrize(
    "denial",
    [
        psutil.AccessDenied(pid=123, name="private-secret-process"),
        PermissionError("private network detail token=secret"),
    ],
)
def test_connection_denial_preserves_adapter_and_io_with_sanitized_metadata(
    monkeypatch: pytest.MonkeyPatch,
    denial: Exception,
) -> None:
    def deny(kind: str) -> list[object]:
        raise denial

    install_network_fakes(monkeypatch, deny)

    result = NetworkCollector().collect()

    assert result.status is CollectorStatus.PARTIAL
    assert result.error_code == "network_connections_denied"
    assert result.error_message == "network connections unavailable"
    assert result.data["adapters"]
    assert result.data["connections"] == []
    assert result.data["io"]["bytes_recv"] == 2
    assert "secret" not in repr(result)
    assert "private" not in repr(result)


def test_disabled_network_performs_no_psutil_probes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def executed(*args: object, **kwargs: object) -> object:
        raise AssertionError("executed")

    monkeypatch.setattr("monitor_agent.collectors.network.psutil.net_if_addrs", executed)
    monkeypatch.setattr("monitor_agent.collectors.network.psutil.net_if_stats", executed)
    monkeypatch.setattr("monitor_agent.collectors.network.psutil.net_io_counters", executed)
    monkeypatch.setattr("monitor_agent.collectors.network.psutil.net_connections", executed)

    result = NetworkCollector(enabled=False).collect()

    assert result.status is CollectorStatus.DISABLED
    assert result.data == {"adapters": [], "connections": [], "io": {}}


def test_adapter_and_connection_order_is_deterministic(monkeypatch) -> None:
    link_family = getattr(psutil, "AF_LINK", object())
    monkeypatch.setattr(
        "monitor_agent.collectors.network.psutil.net_if_addrs",
        lambda: {
            "zeta": [SimpleNamespace(family=socket.AF_INET, address="10.0.0.9")],
            "Alpha": [
                SimpleNamespace(family=socket.AF_INET, address="10.0.0.1"),
                SimpleNamespace(family=link_family, address="aa:bb:cc:dd:ee:ff"),
            ],
        },
    )
    monkeypatch.setattr(
        "monitor_agent.collectors.network.psutil.net_if_stats",
        lambda: {
            "zeta": SimpleNamespace(isup=True, speed=100, mtu=1400),
            "Alpha": SimpleNamespace(isup=True, speed=1000, mtu=1500),
        },
    )
    monkeypatch.setattr(
        "monitor_agent.collectors.network.psutil.net_io_counters",
        lambda: None,
    )
    connections = [
        SimpleNamespace(
            fd=9,
            family=2,
            type=2,
            laddr=("10.0.0.9", 9),
            raddr=(),
            status="NONE",
            pid=9,
        ),
        SimpleNamespace(
            fd=1,
            family=2,
            type=1,
            laddr=("10.0.0.1", 1),
            raddr=(),
            status="LISTEN",
            pid=1,
        ),
    ]
    monkeypatch.setattr(
        "monitor_agent.collectors.network.psutil.net_connections",
        lambda kind: connections,
    )

    result = NetworkCollector().collect()

    assert [item["interface"] for item in result.data["adapters"]] == [
        "Alpha",
        "zeta",
    ]
    assert [item["fd"] for item in result.data["connections"]] == [1, 9]
    assert result.data["connections"][0]["family"] == "2"
    assert result.data["io"] == {}


def test_network_handles_missing_af_link_malformed_addresses_and_unsafe_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class SecretValue:
        def __repr__(self) -> str:
            return "private-secret"

        def __str__(self) -> str:
            return "private-secret"

    monkeypatch.delattr(psutil, "AF_LINK", raising=False)
    monkeypatch.setattr(
        "monitor_agent.collectors.network.psutil.net_if_addrs",
        lambda: {
            "eth0": [
                SimpleNamespace(family=socket.AF_INET, address=SecretValue()),
                SimpleNamespace(family=999, address=SecretValue()),
            ]
        },
    )
    monkeypatch.setattr(
        "monitor_agent.collectors.network.psutil.net_if_stats",
        lambda: {
            "eth0": SimpleNamespace(
                isup=True,
                speed=float("nan"),
                mtu=SecretValue(),
            )
        },
    )
    monkeypatch.setattr(
        "monitor_agent.collectors.network.psutil.net_io_counters",
        lambda: SimpleNamespace(
            bytes_sent=SecretValue(),
            bytes_recv=float("inf"),
            packets_sent=3,
            packets_recv=4,
            errin=0,
            errout=0,
        ),
    )
    monkeypatch.setattr(
        "monitor_agent.collectors.network.psutil.net_connections",
        lambda kind: [
            SimpleNamespace(
                fd=True,
                family=socket.AF_INET6,
                type=socket.SOCK_STREAM,
                laddr=(SecretValue(), 80),
                raddr=("::1", "not-a-port"),
                status=SecretValue(),
                pid=SecretValue(),
            ),
            object(),
        ],
    )

    result = NetworkCollector().collect()

    assert result.status is CollectorStatus.SUCCESS
    assert result.data["adapters"] == [
        {
            "interface": "eth0",
            "ipv4": None,
            "mac": None,
            "speed_mb": None,
            "mtu": None,
        }
    ]
    assert result.data["connections"] == [
        {
            "fd": None,
            "family": "AF_INET6",
            "type": "SOCK_STREAM",
            "laddr": None,
            "raddr": None,
            "status": None,
            "pid": None,
        }
    ]
    assert result.data["io"] == {
        "bytes_sent": None,
        "bytes_recv": None,
        "packets_sent": 3,
        "packets_recv": 4,
        "errin": 0,
        "errout": 0,
    }
    assert "private-secret" not in repr(result)
    json.dumps(result.data, allow_nan=False)
