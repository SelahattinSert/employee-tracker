from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from monitor_agent.collectors import users as users_module
from monitor_agent.collectors.users import UsersCollector
from monitor_agent.models import CollectorStatus


def test_users_preserve_existing_schema(monkeypatch: pytest.MonkeyPatch) -> None:
    sessions = [
        SimpleNamespace(
            name="employee",
            terminal="pts/1",
            host="10.0.0.4",
            started=1_700_000_000.0,
            pid=42,
        ),
        SimpleNamespace(
            name="local",
            terminal=None,
            host=None,
            started=0,
            pid=None,
        ),
    ]
    monkeypatch.setattr(users_module.psutil, "users", lambda: sessions)

    collector = UsersCollector()
    result = collector.collect()

    assert collector.name == "users"
    assert result.status is CollectorStatus.SUCCESS
    assert result.data == {
        "users": [
            {
                "name": "employee",
                "terminal": "pts/1",
                "host": "10.0.0.4",
                "started": "2023-11-14T22:13:20+00:00",
                "pid": 42,
            },
            {
                "name": "local",
                "terminal": None,
                "host": None,
                "started": "1970-01-01T00:00:00+00:00",
                "pid": None,
            },
        ]
    }
    json.dumps(result.data, allow_nan=False)


@pytest.mark.parametrize(
    "error",
    [
        PermissionError("private user path token=secret"),
        OSError("private operating-system detail token=secret"),
    ],
)
def test_user_access_failure_is_sanitized_partial(
    monkeypatch: pytest.MonkeyPatch,
    error: OSError,
) -> None:
    def fail() -> list[object]:
        raise error

    monkeypatch.setattr(users_module.psutil, "users", fail)

    result = UsersCollector().collect()

    assert result.status is CollectorStatus.PARTIAL
    assert result.error_code == "user_access_partial"
    assert result.error_message == "user sessions unavailable"
    assert result.data == {"users": []}
    assert "secret" not in repr(result)
    assert "private" not in repr(result)


def test_non_string_and_missing_user_values_are_safe_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class SecretValue:
        def __str__(self) -> str:
            return "do-not-print-secret"

        def __repr__(self) -> str:
            return "do-not-print-secret"

    monkeypatch.setattr(
        users_module.psutil,
        "users",
        lambda: [
            SimpleNamespace(
                name=SecretValue(),
                terminal=SecretValue(),
                host=SecretValue(),
                started=None,
                pid=SecretValue(),
            )
        ],
    )

    result = UsersCollector().collect()

    assert result.data == {
        "users": [
            {
                "name": None,
                "terminal": None,
                "host": None,
                "started": None,
                "pid": None,
            }
        ]
    }
    assert "do-not-print-secret" not in repr(result)
    json.dumps(result.data, allow_nan=False)
