from pathlib import Path

WORKFLOW_PATH = Path(__file__).parents[1] / ".github" / "workflows" / "ci.yml"


def test_deployment_jobs_parse_every_shell_script_individually() -> None:
    workflow = WORKFLOW_PATH.read_text(encoding="utf-8")

    assert (
        "for script in deploy/linux/install.sh deploy/linux/uninstall.sh; do\n"
        '            bash -n "$script" || exit $?\n'
        "          done"
    ) in workflow
    assert (
        "for script in deploy/macos/install.sh deploy/macos/run-agent.sh "
        "deploy/macos/uninstall.sh; do\n"
        '            sh -n "$script" || exit $?\n'
        "          done"
    ) in workflow


def test_powershell_parser_uses_the_scheduled_task_shell() -> None:
    workflow = WORKFLOW_PATH.read_text(encoding="utf-8")

    assert "name: Parse PowerShell deployment scripts\n        shell: powershell" in workflow
