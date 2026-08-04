from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from uuid import UUID

import pytest

from monitor_agent.collectors.network import NetworkCollector
from monitor_agent.identity import MachineIdentity
from monitor_agent.models import CollectionBatch, CollectorResult, CollectorStatus
from monitor_agent.payload import build_payload


def test_payload_emits_v1_1_schema_and_adds_agent_metadata() -> None:
    batch = CollectionBatch(
        results=(
            CollectorResult("system", CollectorStatus.SUCCESS, 4, {"system": {"hostname": "host"}}),
            CollectorResult(
                "processes",
                CollectorStatus.FAILED,
                7,
                {},
                "permission_error",
                "collector access denied",
            ),
        ),
        duration_ms=11,
    )

    payload = build_payload(
        "heartbeat",
        MachineIdentity("machine-uuid", "linux-machine-id"),
        batch,
        now=datetime(2026, 7, 20, 12, 0, tzinfo=UTC),
        event_id=UUID("12345678-1234-5678-9234-567812345678"),
    )

    assert payload["schema_version"] == "1.1"
    assert set(
        [
            "event",
            "timestamp",
            "machine_id",
            "system",
            "users",
            "cpu",
            "memory",
            "disks",
            "network",
            "processes",
            "software",
            "active_window",
            "file_audit",
            "screenshot",
        ]
    ).issubset(payload)
    assert payload["event_id"] == "12345678-1234-5678-9234-567812345678"
    assert payload["processes"] == []
    assert payload["agent"]["version"] == "2.0.0"
    assert payload["agent"]["collection_duration_ms"] == 11
    assert payload["agent"]["collectors"]["processes"] == {
        "status": "failed",
        "duration_ms": 7,
        "error_code": "permission_error",
        "error_message": "collector access denied",
    }


@pytest.mark.parametrize("event", ["", "Heartbeat", "bad event", "a" * 65])
def test_payload_rejects_invalid_event_names(event: str) -> None:
    with pytest.raises(ValueError, match=r"^invalid event name$"):
        build_payload(event, MachineIdentity("machine", "source"), CollectionBatch((), 0))


def test_payload_rejects_naive_timestamp() -> None:
    with pytest.raises(ValueError, match=r"^now must be timezone-aware$"):
        build_payload(
            "heartbeat",
            MachineIdentity("machine", "source"),
            CollectionBatch((), 0),
            now=datetime(2026, 7, 20, 12, 0),
        )


def test_payload_normalizes_aware_timestamp_to_utc() -> None:
    payload = build_payload(
        "heartbeat",
        MachineIdentity("machine", "source"),
        CollectionBatch((), 0),
        now=datetime(2026, 7, 20, 15, 30, tzinfo=timezone(timedelta(hours=3))),
        event_id=UUID("12345678-1234-5678-9234-567812345678"),
    )

    assert payload["timestamp"] == "2026-07-20T12:30:00+00:00"


def test_payload_generates_a_uuid4_when_event_id_is_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    generated = UUID("12345678-1234-4678-9234-567812345678")
    calls = 0

    def fake_uuid4() -> UUID:
        nonlocal calls
        calls += 1
        return generated

    monkeypatch.setattr("monitor_agent.payload.uuid4", fake_uuid4)
    payload = build_payload(
        "heartbeat", MachineIdentity("machine", "source"), CollectionBatch((), 0)
    )

    assert payload["event_id"] == str(generated)
    assert calls == 1


def test_payload_merges_only_known_sections_from_success_and_partial_mapping_data() -> None:
    system_data = {"system": {"hostname": "host"}, "unexpected": "ignored"}
    network_data = {"network": {"adapters": [{"name": "eth0"}]}}
    active_window_data = {
        "active_window": {"title": "Quarterly report", "app": "writer", "pid": 42}
    }
    file_audit_data = {
        "file_audit": [
            {
                "path": "/srv/reports/q4.pdf",
                "size_bytes": 128,
                "modified": "2026-08-04T09:30:00+00:00",
                "sha256": "a" * 64,
            }
        ]
    }
    screenshot_data = {
        "screenshot": {
            "captured_at": "2026-08-04T09:30:00+00:00",
            "format": "image/png",
            "size_bytes": 3,
            "data_b64": "UE5H",
        }
    }
    batch = CollectionBatch(
        (
            CollectorResult("system", CollectorStatus.SUCCESS, 1, system_data),
            CollectorResult("network", CollectorStatus.PARTIAL, 2, network_data),
            CollectorResult("active_window", CollectorStatus.SUCCESS, 2, active_window_data),
            CollectorResult("file_audit", CollectorStatus.SUCCESS, 2, file_audit_data),
            CollectorResult("screenshot", CollectorStatus.SUCCESS, 2, screenshot_data),
            CollectorResult("cpu", CollectorStatus.SUCCESS, 3, ["not a mapping"]),
            CollectorResult("software", CollectorStatus.FAILED, 4, {"software": ["leak"]}),
            CollectorResult("users", CollectorStatus.TIMED_OUT, 5, {"users": ["leak"]}),
            CollectorResult("disks", CollectorStatus.DISABLED, 6, {"disks": ["leak"]}),
        ),
        21,
    )

    payload = build_payload("heartbeat", MachineIdentity("machine", "source"), batch)

    assert payload["system"] == {"hostname": "host"}
    assert payload["network"] == {"adapters": [{"name": "eth0"}]}
    assert payload["cpu"] == {}
    assert payload["software"] == []
    assert payload["users"] == []
    assert payload["disks"] == []
    assert payload["active_window"] == active_window_data["active_window"]
    assert payload["file_audit"] == file_audit_data["file_audit"]
    assert payload["screenshot"] == screenshot_data["screenshot"]
    assert "unexpected" not in payload


def test_payload_includes_network_collector_output(monkeypatch: pytest.MonkeyPatch) -> None:
    expected_network = {
        "adapters": [{"interface": "eth0"}],
        "connections": [],
        "io": {"bytes_recv": 42},
    }
    monkeypatch.setattr(
        "monitor_agent.collectors.network._collect_adapters",
        lambda: expected_network["adapters"],
    )
    monkeypatch.setattr(
        "monitor_agent.collectors.network._collect_io",
        lambda: expected_network["io"],
    )
    monkeypatch.setattr(
        "monitor_agent.collectors.network.psutil.net_connections",
        lambda kind: [],
    )

    collector_payload = NetworkCollector().collect()
    batch = CollectionBatch(
        (
            CollectorResult(
                NetworkCollector.name,
                collector_payload.status,
                1,
                collector_payload.data,
                collector_payload.error_code,
                collector_payload.error_message,
            ),
        ),
        1,
    )

    payload = build_payload("heartbeat", MachineIdentity("machine", "source"), batch)

    assert payload["network"] == expected_network


def test_payload_keeps_defaults_and_inputs_independent_for_duplicate_and_unknown_collectors() -> (
    None
):
    first_data = {"system": {"hostname": "first"}}
    second_data = {"system": {"hostname": "second"}}
    batch = CollectionBatch(
        (
            CollectorResult("system", CollectorStatus.SUCCESS, 1, first_data),
            CollectorResult("system", CollectorStatus.SUCCESS, 2, second_data),
            CollectorResult("unknown", CollectorStatus.SUCCESS, 3, {"users": ["user"]}),
        ),
        6,
    )

    payload = build_payload("heartbeat", MachineIdentity("machine-id", "source"), batch)
    payload["system"]["hostname"] = "changed"
    payload["network"]["adapters"].append({"name": "loopback"})

    assert first_data == {"system": {"hostname": "first"}}
    assert second_data == {"system": {"hostname": "second"}}
    assert payload["system"] == {"hostname": "changed"}
    assert payload["users"] == ["user"]
    assert set(payload["agent"]["collectors"]) == {"system", "unknown"}


def test_payload_records_all_collector_statuses_and_machine_identity_without_source() -> None:
    statuses = tuple(CollectorStatus)
    batch = CollectionBatch(
        tuple(CollectorResult(status.value, status, 1, {}) for status in statuses),
        len(statuses),
    )

    payload = build_payload("heartbeat", MachineIdentity("machine-id", "identity-source"), batch)

    assert payload["machine_id"] == "machine-id"
    non_agent_payload = {key: value for key, value in payload.items() if key != "agent"}
    assert "identity-source" not in str(non_agent_payload)
    assert payload["agent"]["identity_source"] == "identity-source"
    collector_statuses = {
        name: details["status"] for name, details in payload["agent"]["collectors"].items()
    }
    assert collector_statuses == {status.value: status.value for status in statuses}
