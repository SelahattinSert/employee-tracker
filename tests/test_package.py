import argparse
import platform
import shlex
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from monitor_agent import __version__
from monitor_agent.cli import entrypoint, main


def test_version_constant() -> None:
    assert __version__ == "2.0.0"


def test_version_command(capsys) -> None:
    assert main(["version"]) == 0
    assert capsys.readouterr().out.strip() == (
        f"monitor-agent 2.0.0 Python {platform.python_version()}"
    )


def test_main_rejects_unhandled_command(monkeypatch) -> None:
    class FakeParser:
        def parse_args(self, argv: object) -> argparse.Namespace:
            assert argv == ["unexpected"]
            return argparse.Namespace(command="unexpected")

    monkeypatch.setattr("monitor_agent.cli.build_parser", FakeParser)

    with pytest.raises(AssertionError) as exc_info:
        main(["unexpected"])

    assert str(exc_info.value) == "unhandled command: unexpected"


def test_pytest_enforces_line_and_branch_coverage() -> None:
    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    config = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    addopts = shlex.split(config["tool"]["pytest"]["ini_options"]["addopts"])

    assert "--cov=monitor_agent" in addopts
    assert "--cov-branch" in addopts
    assert "--cov-report=term-missing" in addopts
    assert "--cov-fail-under=90" in addopts


def test_entrypoint_exits_with_success(monkeypatch, capsys) -> None:
    monkeypatch.setattr(sys, "argv", ["monitor-agent", "version"])

    with pytest.raises(SystemExit) as exc_info:
        entrypoint()

    captured = capsys.readouterr()
    assert exc_info.value.code == 0
    assert captured.out.strip() == (
        f"monitor-agent 2.0.0 Python {platform.python_version()}"
    )
    assert captured.err == ""


def test_python_m_monitor_agent() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "monitor_agent", "version"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert result.stdout.strip() == (
        f"monitor-agent 2.0.0 Python {platform.python_version()}"
    )
    assert result.stderr == ""
