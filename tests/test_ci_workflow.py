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


def test_systemd_unit_is_verified_against_staged_runtime_paths() -> None:
    workflow = WORKFLOW_PATH.read_text(encoding="utf-8")

    assert (
        "name: Verify systemd unit against staged runtime\n"
        "        run: |\n"
        '          staged_root="$(mktemp -d)"\n'
        "          install -Dm644 deploy/linux/monitor-agent.service "
        '"$staged_root/etc/systemd/system/monitor-agent.service"\n'
        "          install -Dm755 /bin/true "
        '"$staged_root/opt/monitor-agent/venv/bin/monitor-agent"\n'
        "          install -Dm600 /dev/null "
        '"$staged_root/etc/monitor-agent/monitor-agent.env"\n'
        "          for target in sysinit.target network-online.target multi-user.target; do\n"
        '            target_path="$staged_root/usr/lib/systemd/system/$target"\n'
        '            install -Dm644 /dev/null "$target_path"\n'
        "            printf '%s\\n' '[Unit]' \"Description=staged $target\" > \"$target_path\"\n"
        "          done\n"
        "          systemd-analyze verify --root="
        '"$staged_root" --recursive-errors=yes '
        "/etc/systemd/system/monitor-agent.service"
    ) in workflow
    assert "systemd-analyze verify deploy/linux/monitor-agent.service" not in workflow
    assert 'for target in sysinit.target network-online.target multi-user.target; do' in workflow


def test_matrix_test_job_excludes_platform_deployment_harnesses() -> None:
    workflow = WORKFLOW_PATH.read_text(encoding="utf-8")

    assert "python -m pytest --ignore=tests/deploy" in workflow
