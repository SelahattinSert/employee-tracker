from monitor_agent import __version__
from monitor_agent.cli import main


def test_version_constant() -> None:
    assert __version__ == "2.0.0"


def test_version_command(capsys) -> None:
    assert main(["version"]) == 0
    assert capsys.readouterr().out.strip() == "monitor-agent 2.0.0"
