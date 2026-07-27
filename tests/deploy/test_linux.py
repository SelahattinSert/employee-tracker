from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
DEPLOY = ROOT / "deploy" / "linux"
SERVICE = DEPLOY / "monitor-agent.service"
INSTALLER = DEPLOY / "install.sh"
UNINSTALLER = DEPLOY / "uninstall.sh"
ENV_EXAMPLE = DEPLOY / "monitor-agent.env.example"

EXPECTED_SERVICE_LINES = {
    "Description=Monitor Agent 2.0 Endpoint Telemetry",
    "Documentation=file:/opt/monitor-agent/README.md",
    "After=network-online.target",
    "Wants=network-online.target",
    "Type=simple",
    "EnvironmentFile=/etc/monitor-agent/monitor-agent.env",
    "ExecStartPre=/opt/monitor-agent/venv/bin/monitor-agent check-config",
    "ExecStart=/opt/monitor-agent/venv/bin/monitor-agent run",
    "Restart=on-failure",
    "RestartSec=30",
    "TimeoutStopSec=45",
    "StateDirectory=monitor-agent",
    "StateDirectoryMode=0700",
    "NoNewPrivileges=true",
    "ProtectSystem=strict",
    "ProtectHome=read-only",
    "PrivateTmp=true",
    "PrivateDevices=true",
    "ProtectKernelTunables=true",
    "ProtectKernelModules=true",
    "ProtectControlGroups=true",
    "RestrictSUIDSGID=true",
    "LockPersonality=true",
    "MemoryDenyWriteExecute=true",
    "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6",
    "SystemCallArchitectures=native",
    "UMask=0077",
    "ReadWritePaths=/var/lib/monitor-agent",
    "StandardOutput=journal",
    "StandardError=journal",
    "WantedBy=multi-user.target",
}

EXPECTED_ENVIRONMENT = [
    "MONITOR_COLLECTOR_URI=https://collector.internal/api/v1/telemetry",
    "MONITOR_API_TOKEN=replace-with-managed-secret",
    "MONITOR_HEARTBEAT_SEC=300",
    "MONITOR_STARTUP_DELAY_SEC=30",
    "MONITOR_SPOOL_PATH=/var/lib/monitor-agent/spool",
    "MONITOR_PROCESS_CMDLINE_MODE=redacted",
    "MONITOR_LOG_FORMAT=json",
]


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def _fake_commands(
    tmp_path: Path,
    *,
    uid: int = 0,
    implementation: str = "CPython",
    major: int = 3,
    minor: int = 11,
    systemctl_failure: str = "",
) -> tuple[Path, Path]:
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir(parents=True)
    log = tmp_path / "commands.log"
    _write_executable(
        fake_bin / "id",
        f"""#!/usr/bin/env bash
set -eu
if [ "$#" -eq 1 ] && [ "$1" = "-u" ]; then
    printf '%s\\n' '{uid}'
    exit 0
fi
exit 64
""",
    )
    _write_executable(
        fake_bin / "python3.11",
        f"""#!/usr/bin/env bash
set -eu
printf 'python' >> "$FAKE_COMMAND_LOG"
printf ' <%s>' "$@" >> "$FAKE_COMMAND_LOG"
printf '\\n' >> "$FAKE_COMMAND_LOG"
if [ "${{1-}}" = "-c" ]; then
    printf '%s %s %s\\n' '{implementation}' '{major}' '{minor}'
elif [ "${{1-}}" = "-m" ] && [ "${{2-}}" = "venv" ]; then
    target="${{3:?}}"
    mkdir -p -- "$target/bin"
    cp -- "$0" "$target/bin/python"
fi
""",
    )
    _write_executable(
        fake_bin / "systemctl",
        """#!/usr/bin/env bash
set -eu
printf 'systemctl' >> "$FAKE_COMMAND_LOG"
printf ' <%s>' "$@" >> "$FAKE_COMMAND_LOG"
printf '\\n' >> "$FAKE_COMMAND_LOG"
if [ -n "${FAKE_SYSTEMCTL_FAILURE-}" ] && [ "${1-}" = "$FAKE_SYSTEMCTL_FAILURE" ]; then
    exit 1
fi
""",
    )
    return fake_bin, log


def _deployment_copy(tmp_path: Path, *, readme: bool = False) -> Path:
    project = tmp_path / "project"
    linux = project / "deploy" / "linux"
    linux.mkdir(parents=True)
    shutil.copy2(INSTALLER, linux / "install.sh")
    shutil.copy2(SERVICE, linux / "monitor-agent.service")
    shutil.copy2(ROOT / "requirements.lock", project / "requirements.lock")
    if readme:
        (project / "README.md").write_text("deployment guide\n", encoding="utf-8")
    return project


def _installer_inputs(tmp_path: Path) -> tuple[Path, Path]:
    wheel = tmp_path / "monitor_agent-2.0.0-py3-none-any.whl"
    wheel.write_bytes(b"wheel fixture")
    environment = tmp_path / "supplied.env"
    environment.write_text(
        "MONITOR_API_TOKEN=top-secret-test-token\n"
        "MONITOR_SPOOL_PATH=/var/lib/monitor-agent/spool\n",
        encoding="utf-8",
    )
    environment.chmod(0o644)
    return wheel, environment


def _run(
    script: Path,
    *arguments: Path | str,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(script), *(str(argument) for argument in arguments)],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )


def _install_environment(
    tmp_path: Path,
    fake_bin: Path,
    log: Path,
    stage_root: Path,
    *,
    failure: str = "",
) -> dict[str, str]:
    return {
        **os.environ,
        "PATH": f"{fake_bin}:/usr/bin:/bin",
        "FAKE_COMMAND_LOG": str(log),
        "FAKE_SYSTEMCTL_FAILURE": failure,
        "MONITOR_AGENT_TEST_ROOT": str(stage_root),
    }


def test_service_has_exact_runtime_hardening_and_lifecycle_contract() -> None:
    lines = {
        line.strip()
        for line in SERVICE.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith(("#", "["))
    }
    assert lines == EXPECTED_SERVICE_LINES


def test_service_contains_no_inline_secret_or_live_configuration() -> None:
    text = SERVICE.read_text(encoding="utf-8")
    assert "User=" not in text
    assert "Environment=" not in text
    assert "API_TOKEN" not in text
    assert "COLLECTOR_URI" not in text
    assert "LOG_PATH" not in text
    assert "__REPLACE__" not in text


def test_scripts_are_bash_strict_and_executable() -> None:
    for script in (INSTALLER, UNINSTALLER):
        lines = script.read_text(encoding="utf-8").splitlines()
        assert lines[:2] == ["#!/usr/bin/env bash", "set -eu"]
        assert script.stat().st_mode & stat.S_IXUSR


def test_installer_has_root_and_exact_argument_guards(tmp_path: Path) -> None:
    fake_bin, log = _fake_commands(tmp_path, uid=1000)
    wheel, environment = _installer_inputs(tmp_path)
    env = {**os.environ, "PATH": f"{fake_bin}:/usr/bin:/bin", "FAKE_COMMAND_LOG": str(log)}

    non_root = _run(INSTALLER, wheel, environment, env=env)
    assert non_root.returncode == 2
    assert non_root.stderr == "monitor-agent install: root privileges required\n"
    assert "top-secret-test-token" not in non_root.stdout + non_root.stderr

    fake_bin, log = _fake_commands(tmp_path / "root", uid=0)
    env.update(PATH=f"{fake_bin}:/usr/bin:/bin", FAKE_COMMAND_LOG=str(log))
    wrong_arguments = _run(INSTALLER, wheel, env=env)
    assert wrong_arguments.returncode == 2
    assert wrong_arguments.stderr == "monitor-agent install: expected WHEEL_PATH ENV_FILE\n"


@pytest.mark.parametrize(
    ("implementation", "major", "minor", "accepted"),
    [
        ("CPython", 3, 10, False),
        ("CPython", 3, 11, True),
        ("CPython", 3, 12, True),
        ("CPython", 3, 13, True),
        ("CPython", 3, 14, True),
        ("CPython", 3, 15, False),
        ("PyPy", 3, 11, False),
        ("CPython", 4, 11, False),
    ],
)
def test_installer_accepts_only_supported_cpython(
    tmp_path: Path,
    implementation: str,
    major: int,
    minor: int,
    accepted: bool,
) -> None:
    project = _deployment_copy(tmp_path)
    wheel, environment = _installer_inputs(tmp_path)
    fake_bin, log = _fake_commands(
        tmp_path,
        implementation=implementation,
        major=major,
        minor=minor,
    )
    stage_root = tmp_path / "stage"
    env = _install_environment(tmp_path, fake_bin, log, stage_root)
    result = _run(project / "deploy/linux/install.sh", wheel, environment, env=env)

    assert (result.returncode == 0) is accepted
    if not accepted:
        assert result.stderr == "monitor-agent install: unsupported Python interpreter\n"
        assert not stage_root.exists()


def test_installer_stages_fresh_hash_locked_runtime_and_activates_service(
    tmp_path: Path,
) -> None:
    project = _deployment_copy(tmp_path, readme=True)
    wheel, environment = _installer_inputs(tmp_path)
    fake_bin, log = _fake_commands(tmp_path)
    stage_root = tmp_path / "stage"
    env = _install_environment(tmp_path, fake_bin, log, stage_root)

    result = _run(project / "deploy/linux/install.sh", wheel, environment, env=env)

    assert result.returncode == 0, result.stderr
    commands = log.read_text(encoding="utf-8").splitlines()
    assert any(
        " <-m> <pip> <install> <--require-hashes> <-r> "
        f"<{project / 'requirements.lock'}>" in command
        for command in commands
    )
    assert any(
        " <-m> <pip> <install> <--no-deps> <--force-reinstall> "
        f"<--> <{wheel}>" in command
        for command in commands
    )
    assert not any("upgrade" in command and "pip" in command for command in commands)
    assert sum("<-m> <venv>" in command for command in commands) == 1
    assert commands[-4:] == [
        "systemctl <daemon-reload>",
        "systemctl <enable> <--> <monitor-agent.service>",
        "systemctl <restart> <--> <monitor-agent.service>",
        "systemctl <is-active> <--quiet> <--> <monitor-agent.service>",
    ]

    install_root = stage_root / "opt" / "monitor-agent"
    assert (install_root / "venv/bin/python").is_file()
    assert (install_root / "README.md").read_text(encoding="utf-8") == "deployment guide\n"
    assert stat.S_IMODE((stage_root / "etc/monitor-agent").stat().st_mode) == 0o700
    assert stat.S_IMODE(
        (stage_root / "etc/monitor-agent/monitor-agent.env").stat().st_mode
    ) == 0o600
    assert stat.S_IMODE((stage_root / "var/lib/monitor-agent").stat().st_mode) == 0o700
    assert stat.S_IMODE(
        (stage_root / "etc/systemd/system/monitor-agent.service").stat().st_mode
    ) == 0o644
    assert not list((stage_root / "opt").glob(".monitor-agent-install.*"))
    assert "top-secret-test-token" not in result.stdout + result.stderr


def test_installer_never_executes_environment_and_readme_is_optional(tmp_path: Path) -> None:
    project = _deployment_copy(tmp_path)
    wheel, environment = _installer_inputs(tmp_path)
    side_effect = tmp_path / "environment-was-executed"
    environment.write_text(
        f"MONITOR_API_TOKEN=$(touch {side_effect})\n",
        encoding="utf-8",
    )
    fake_bin, log = _fake_commands(tmp_path)
    stage_root = tmp_path / "stage"
    env = _install_environment(tmp_path, fake_bin, log, stage_root)

    result = _run(project / "deploy/linux/install.sh", wheel, environment, env=env)

    assert result.returncode == 0, result.stderr
    assert not side_effect.exists()
    assert not (stage_root / "opt/monitor-agent/README.md").exists()
    installed = stage_root / "etc/monitor-agent/monitor-agent.env"
    assert installed.read_text(encoding="utf-8") == environment.read_text(encoding="utf-8")


def test_installer_failure_rolls_back_runtime_and_config_but_preserves_state(
    tmp_path: Path,
) -> None:
    project = _deployment_copy(tmp_path)
    wheel, environment = _installer_inputs(tmp_path)
    fake_bin, log = _fake_commands(tmp_path)
    stage_root = tmp_path / "stage"
    runtime = stage_root / "opt/monitor-agent"
    config = stage_root / "etc/monitor-agent/monitor-agent.env"
    state = stage_root / "var/lib/monitor-agent"
    unit = stage_root / "etc/systemd/system/monitor-agent.service"
    runtime.mkdir(parents=True)
    config.parent.mkdir(parents=True)
    state.mkdir(parents=True)
    unit.parent.mkdir(parents=True)
    (runtime / "old-runtime").write_text("keep\n", encoding="utf-8")
    config.write_text("MONITOR_API_TOKEN=old\n", encoding="utf-8")
    (state / "telemetry.json").write_text("keep\n", encoding="utf-8")
    unit.write_text("old unit\n", encoding="utf-8")
    env = _install_environment(tmp_path, fake_bin, log, stage_root, failure="restart")

    result = _run(project / "deploy/linux/install.sh", wheel, environment, env=env)

    assert result.returncode != 0
    assert (runtime / "old-runtime").read_text(encoding="utf-8") == "keep\n"
    assert config.read_text(encoding="utf-8") == "MONITOR_API_TOKEN=old\n"
    assert (state / "telemetry.json").read_text(encoding="utf-8") == "keep\n"
    assert unit.read_text(encoding="utf-8") == "old unit\n"
    assert not list((stage_root / "opt").glob(".monitor-agent-install.*"))
    assert "top-secret-test-token" not in result.stdout + result.stderr


def test_installer_validates_all_inputs_before_mutating(tmp_path: Path) -> None:
    project = _deployment_copy(tmp_path)
    _, environment = _installer_inputs(tmp_path)
    missing_wheel = tmp_path / "missing.whl"
    fake_bin, log = _fake_commands(tmp_path)
    stage_root = tmp_path / "stage"
    env = _install_environment(tmp_path, fake_bin, log, stage_root)

    result = _run(
        project / "deploy/linux/install.sh",
        missing_wheel,
        environment,
        env=env,
    )

    assert result.returncode != 0
    assert result.stderr == "monitor-agent install: invalid wheel\n"
    assert not stage_root.exists()


def test_uninstaller_guards_arguments_and_uses_fixed_production_targets(
    tmp_path: Path,
) -> None:
    fake_bin, log = _fake_commands(tmp_path, uid=1000)
    env = {**os.environ, "PATH": f"{fake_bin}:/usr/bin:/bin", "FAKE_COMMAND_LOG": str(log)}
    non_root = _run(UNINSTALLER, env=env)
    assert non_root.returncode == 2
    assert non_root.stderr == "monitor-agent uninstall: root privileges required\n"

    text = UNINSTALLER.read_text(encoding="utf-8")
    assert "rm -rf -- /opt/monitor-agent" in text
    assert "rm -f -- /etc/systemd/system/monitor-agent.service" in text
    assert "rm -rf -- /etc/monitor-agent /var/lib/monitor-agent" in text
    assert "rm -rf -- $" not in text
    assert "*" not in text


def test_uninstaller_preserves_state_by_default_and_purges_only_explicitly(
    tmp_path: Path,
) -> None:
    fake_bin, log = _fake_commands(tmp_path)
    _write_executable(
        fake_bin / "rm",
        """#!/usr/bin/env bash
set -eu
printf 'rm' >> "$FAKE_COMMAND_LOG"
printf ' <%s>' "$@" >> "$FAKE_COMMAND_LOG"
printf '\\n' >> "$FAKE_COMMAND_LOG"
""",
    )
    env = {**os.environ, "PATH": f"{fake_bin}:/usr/bin:/bin", "FAKE_COMMAND_LOG": str(log)}

    default = _run(UNINSTALLER, env=env)
    assert default.returncode == 0, default.stderr
    default_commands = log.read_text(encoding="utf-8")
    assert "/opt/monitor-agent" in default_commands
    assert "/etc/systemd/system/monitor-agent.service" in default_commands
    assert "/etc/monitor-agent" not in default_commands
    assert "/var/lib/monitor-agent" not in default_commands

    log.write_text("", encoding="utf-8")
    purge = _run(UNINSTALLER, "--purge", env=env)
    assert purge.returncode == 0, purge.stderr
    purge_commands = log.read_text(encoding="utf-8")
    assert "rm <-rf> <--> </etc/monitor-agent> </var/lib/monitor-agent>" in purge_commands
    assert "systemctl <disable> <--now> <--> <monitor-agent.service>" in purge_commands
    assert purge_commands.rstrip().endswith("systemctl <daemon-reload>")

    wrong = _run(UNINSTALLER, "--force", env=env)
    assert wrong.returncode == 2
    assert wrong.stderr == "monitor-agent uninstall: expected no arguments or --purge\n"


def test_environment_example_is_exact_and_never_live_configuration() -> None:
    assert ENV_EXAMPLE.read_text(encoding="utf-8").splitlines() == EXPECTED_ENVIRONMENT
    assert ".env.example" not in SERVICE.read_text(encoding="utf-8")
    assert "monitor-agent.env.example" not in INSTALLER.read_text(encoding="utf-8")
