import runpy
from unittest.mock import Mock


def test_legacy_entrypoint_calls_package_entrypoint_once(monkeypatch) -> None:
    entrypoint = Mock()
    monkeypatch.setattr("monitor_agent.cli.entrypoint", entrypoint)

    runpy.run_path("agent/monitor_agent.py", run_name="__main__")

    entrypoint.assert_called_once_with()
