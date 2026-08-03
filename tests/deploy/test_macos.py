from __future__ import annotations

# ruff: noqa: E501
import os
import plistlib
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
MACOS = ROOT / "deploy" / "macos"
PLIST = MACOS / "com.company.monitor-agent.plist"
LAUNCHER = MACOS / "run-agent.sh"
INSTALLER = MACOS / "install.sh"
UNINSTALLER = MACOS / "uninstall.sh"
ENV_EXAMPLE = MACOS / "monitor-agent.env.example"

KNOWN_KEYS = {
    "MONITOR_COLLECTOR_URI",
    "MONITOR_API_TOKEN",
    "MONITOR_CA_BUNDLE",
    "MONITOR_HEARTBEAT_SEC",
    "MONITOR_STARTUP_DELAY_SEC",
    "MONITOR_CONNECT_TIMEOUT_SEC",
    "MONITOR_READ_TIMEOUT_SEC",
    "MONITOR_COLLECTION_TIMEOUT_SEC",
    "MONITOR_MAX_COLLECTOR_WORKERS",
    "MONITOR_SPOOL_PATH",
    "MONITOR_SPOOL_MAX_BYTES",
    "MONITOR_SPOOL_MAX_AGE_SEC",
    "MONITOR_REPLAY_BATCH_SIZE",
    "MONITOR_PROCESS_CMDLINE_MODE",
    "MONITOR_INCLUDE_NETWORK_CONNECTIONS",
    "MONITOR_INCLUDE_SOFTWARE",
    "MONITOR_LOG_PATH",
    "MONITOR_LOG_FORMAT",
    "MONITOR_LOG_LEVEL",
}


def _script(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def _run(path: Path, *args: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["sh", str(path), *args], text=True, capture_output=True, check=False, env=env
    )


def _launcher_harness(tmp_path: Path, environment: str) -> tuple[Path, dict[str, str], Path]:
    runtime = tmp_path / "runtime"
    (runtime / "venv" / "bin").mkdir(parents=True)
    shutil.copy2(LAUNCHER, runtime / "run-agent.sh")
    (runtime / "run-agent.sh").chmod(0o700)
    (runtime / "monitor-agent.env").write_text(environment, encoding="utf-8")
    output = tmp_path / "agent-output"
    _write_executable(
        runtime / "venv" / "bin" / "monitor-agent",
        "#!/bin/sh\n"
        'printf "%s|%s|%s|%s" "$1" "$MONITOR_COLLECTOR_URI" '
        '"$MONITOR_API_TOKEN" "${MONITOR_LOG_LEVEL-unset}" > "$AGENT_OUTPUT"\n',
    )
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    _write_executable(
        fake_bin / "stat",
        "#!/bin/sh\n"
        'case "$2" in\n'
        "  %u) printf '0\\n' ;;\n"
        "  %Lp) printf '600\\n' ;;\n"
        "  *) exit 1 ;;\n"
        "esac\n",
    )
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{fake_bin}:{env['PATH']}",
            "AGENT_OUTPUT": str(output),
            "MONITOR_LOG_LEVEL": "inherited-secret-adjacent-value",
        }
    )
    return runtime / "run-agent.sh", env, output


def _replace_once(text: str, expected: str, replacement: str) -> str:
    assert text.count(expected) == 1, expected
    return text.replace(expected, replacement)


def _deployment_harness(
    tmp_path: Path, *, loaded: bool = False
) -> tuple[Path, Path, Path, dict[str, str], Path]:
    """Run narrowly redirected copies of the real macOS deployment scripts."""
    project = tmp_path / "project"
    macos = project / "deploy" / "macos"
    macos.mkdir(parents=True)
    for source in (PLIST, LAUNCHER, INSTALLER, UNINSTALLER):
        shutil.copy2(source, macos / source.name)
    shutil.copy2(ROOT / "requirements.lock", project / "requirements.lock")
    root = tmp_path / "managed"
    app_parent = root / "Library" / "Application Support"
    install_root = app_parent / "MonitorAgent"
    log_root = root / "Library" / "Logs" / "MonitorAgent"
    plist_target = root / "Library" / "LaunchDaemons" / "com.company.monitor-agent.plist"
    app_parent.mkdir(parents=True)
    log_root.parent.mkdir(parents=True)
    plist_target.parent.mkdir(parents=True)
    replacements = {
        'install_root="/Library/Application Support/MonitorAgent"': f'install_root="{install_root}"',
        'app_parent="/Library/Application Support"': f'app_parent="{app_parent}"',
        'log_root="/Library/Logs/MonitorAgent"': f'log_root="{log_root}"',
        'log_parent="/Library/Logs"': f'log_parent="{log_root.parent}"',
        'spool_root="$install_root/spool"': 'spool_root="$install_root/spool"',
        'plist_parent="/Library/LaunchDaemons"': f'plist_parent="{plist_target.parent}"',
        'plist_target="/Library/LaunchDaemons/com.company.monitor-agent.plist"': (
            f'plist_target="{plist_target}"'
        ),
    }
    installer = macos / "install.sh"
    installer_text = installer.read_text(encoding="utf-8")
    for expected, replacement in replacements.items():
        installer_text = _replace_once(installer_text, expected, replacement)
    installer.write_text(installer_text, encoding="utf-8")
    uninstaller = macos / "uninstall.sh"
    uninstaller_text = uninstaller.read_text(encoding="utf-8")
    for expected, replacement in {
        'install_root="/Library/Application Support/MonitorAgent"': f'install_root="{install_root}"',
        'log_root="/Library/Logs/MonitorAgent"': f'log_root="{log_root}"',
        'plist_target="/Library/LaunchDaemons/com.company.monitor-agent.plist"': (
            f'plist_target="{plist_target}"'
        ),
    }.items():
        uninstaller_text = _replace_once(uninstaller_text, expected, replacement)
    uninstaller.write_text(uninstaller_text, encoding="utf-8")

    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    system_commands = {
        name: shutil.which(name) or f"/usr/bin/{name}"
        for name in ("chmod", "install", "mv", "rm")
    }
    _write_executable(fake_bin / "id", "#!/bin/sh\nprintf '0\\n'\n")
    _write_executable(fake_bin / "chown", "#!/bin/sh\nexit 0\n")
    _write_executable(
        fake_bin / "chmod",
        f'#!/bin/sh\nexec "{system_commands["chmod"]}" "$@"\n',
    )
    _write_executable(
        fake_bin / "install",
        f'#!/bin/sh\nexec "{system_commands["install"]}" "$@"\n',
    )
    _write_executable(
        fake_bin / "mv",
        "#!/bin/sh\n"
        'if [ -n "${FAIL_MV_SOURCE:-}" ] && [ "$1" = "$FAIL_MV_SOURCE" ]; then\n'
        "  exit 1\n"
        "fi\n"
        f'exec "{system_commands["mv"]}" "$@"\n',
    )
    _write_executable(
        fake_bin / "rm",
        "#!/bin/sh\n"
        "rm_target=\n"
        'for rm_argument in "$@"; do rm_target=$rm_argument; done\n'
        'if [ "${FAIL_RM_TRANSACTION:-0}" = 1 ]; then\n'
        '  case "$rm_target" in\n'
        '    "$TRANSACTION_PARENT"/.monitor-agent-transaction.*) exit 1 ;;\n'
        "  esac\n"
        "fi\n"
        f'exec "{system_commands["rm"]}" "$@"\n',
    )
    _write_executable(
        fake_bin / "stat",
        "#!/bin/sh\n"
        'format="$2"\npath="$3"\n'
        'case "$format" in\n'
        "  %u) printf '0\\n' ;;\n"
        "  %Lp) case \"$path\" in *monitor-agent.env) printf '600\\n' ;; *plist) printf '644\\n' ;; *) printf '700\\n' ;; esac ;;\n"
        "  %Su:%Sg:%Lp) case \"$path\" in *monitor-agent.env) printf 'root:wheel:600\\n' ;; *plist) printf 'root:wheel:644\\n' ;; *) printf 'root:wheel:700\\n' ;; esac ;;\n"
        "  *) exit 1 ;;\n"
        "esac\n",
    )
    _write_executable(
        fake_bin / "python3.11",
        "#!/bin/sh\n"
        'if [ "$1" = "-c" ]; then printf "CPython 3 11\\n"; exit 0; fi\n'
        'if [ "$1" = "-m" ] && [ "$2" = "venv" ]; then\n'
        '  mkdir -p "$3/bin"\n'
        '  cp "$0" "$3/bin/python"\n'
        '  chmod 755 "$3/bin/python"\n'
        "  exit 0\n"
        "fi\n"
        'if [ "$1" = "-m" ] && [ "$2" = "pip" ]; then\n'
        '  agent="$(dirname "$0")/monitor-agent"\n'
        "  printf '%s\\n' '#!/bin/sh' 'exit 0' > \"$agent\"\n"
        '  chmod 755 "$agent"\n'
        "  exit 0\n"
        "fi\n"
        "exit 1\n",
    )
    state = tmp_path / "launchd-state"
    state.write_text("1\n" if loaded else "0\n", encoding="utf-8")
    running = tmp_path / "launchd-running"
    running.write_text("1\n" if loaded else "0\n", encoding="utf-8")
    _write_executable(
        fake_bin / "launchctl",
        "#!/bin/sh\n"
        'if [ "${LAUNCHD_UNEXPECTED:-}" = 1 ] && [ "$1" = print ]; then exit 42; fi\n'
        'if [ "${FAIL_STAGE:-}" = "$1" ] && [ ! -e "$LAUNCHD_FAIL_ONCE" ]; then touch "$LAUNCHD_FAIL_ONCE"; exit 1; fi\n'
        'case "$1" in\n'
        '  print) if [ "$(cat "$LAUNCHD_STATE")" = 1 ]; then if [ "$(cat "$LAUNCHD_RUNNING")" = 1 ]; then printf "state = running\\n"; else printf "state = exited\\n"; fi; exit 0; fi; exit 113 ;;\n'
        '  bootout) printf "0\\n" > "$LAUNCHD_STATE"; printf "0\\n" > "$LAUNCHD_RUNNING" ;;\n'
        '  bootstrap) printf "1\\n" > "$LAUNCHD_STATE"; printf "1\\n" > "$LAUNCHD_RUNNING" ;;\n'
        "  enable) : ;;\n"
        '  stop) [ "$2" = com.company.monitor-agent ] || exit 1; printf "0\\n" > "$LAUNCHD_RUNNING" ;;\n'
        "  *) exit 1 ;;\n"
        "esac\n",
    )
    _write_executable(
        fake_bin / "plutil",
        '#!/bin/sh\n[ "${FAIL_STAGE:-}" != plutil ]\n',
    )
    wheel = tmp_path / "monitor_agent-2.0.0-py3-none-any.whl"
    wheel.write_bytes(b"wheel")
    environment = tmp_path / "managed.env"
    environment.write_text("MONITOR_API_TOKEN=new secret=value\n", encoding="utf-8")
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{fake_bin}:{env['PATH']}",
            "MONITOR_AGENT_PYTHON": "python3.11",
            "LAUNCHD_STATE": str(state),
            "LAUNCHD_RUNNING": str(running),
            "LAUNCHD_FAIL_ONCE": str(tmp_path / "launchd-failed-once"),
            "TRANSACTION_PARENT": str(app_parent),
        }
    )
    return installer, uninstaller, install_root, env, log_root


def test_launchdaemon_plist_is_valid_complete_and_secret_free() -> None:
    raw = PLIST.read_bytes()
    assert raw.startswith(b'<?xml version="1.0" encoding="UTF-8"?>')
    assert not raw.startswith(b"\xef\xbb\xbf")
    data = plistlib.loads(raw)
    assert data == {
        "Label": "com.company.monitor-agent",
        "ProgramArguments": [
            "/bin/sh",
            "/Library/Application Support/MonitorAgent/run-agent.sh",
        ],
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "ThrottleInterval": 30,
        "StandardOutPath": "/Library/Logs/MonitorAgent/launchd.stdout.log",
        "StandardErrorPath": "/Library/Logs/MonitorAgent/launchd.stderr.log",
        "ProcessType": "Background",
    }
    text = raw.decode("utf-8")
    assert "MONITOR_" not in text
    assert "token" not in text.lower()
    assert "Python" not in text


def test_launcher_uses_a_nonexecuting_strict_allowlist_parser() -> None:
    text = _script(LAUNCHER)
    assert text.startswith("#!/bin/sh\nset -eu\n")
    assert "cd -P" in text and "pwd -P" in text
    assert '"$install_root/monitor-agent.env"' in text
    assert '"$install_root/venv/bin/monitor-agent"' in text
    assert "source " not in text
    assert "eval " not in text
    assert "xargs" not in text
    assert "env $(" not in text
    assert '. "$config_file"' not in text
    assert "IFS= read -r line" in text
    assert "${line%%=*}" in text and "${line#*=}" in text
    assert 'export "$key=$value"' in text
    assert 'unset "$known_key"' in text
    assert 'exec "$agent" "$command"' in text
    for key in KNOWN_KEYS:
        assert key in text


def test_launcher_parses_data_without_interpreting_spaces_or_equals(tmp_path: Path) -> None:
    launcher, env, output = _launcher_harness(
        tmp_path,
        "\n  # ordinary comment\n"
        "MONITOR_COLLECTOR_URI=https://collector.example/v1?a=b=c\n"
        "MONITOR_API_TOKEN=secret value=with=equals\n",
    )
    result = _run(launcher, "health", env=env)
    assert result.returncode == 0, result.stderr
    assert output.read_text(encoding="utf-8") == (
        "health|https://collector.example/v1?a=b=c|secret value=with=equals|unset"
    )


@pytest.mark.parametrize(
    "environment",
    [
        "MONITOR_UNKNOWN=value\n",
        "MONITOR_API_TOKEN=value\nMONITOR_API_TOKEN=again\n",
        "MONITOR API_TOKEN=value\n",
        "MONITOR_API_TOKEN\n",
        "MONITOR_SPOOL_PATH=/tmp/not-monitor-agent\n",
        "MONITOR_LOG_PATH=/tmp/not-monitor-agent.log\n",
    ],
)
def test_launcher_rejects_malformed_unknown_and_duplicate_entries(
    tmp_path: Path, environment: str
) -> None:
    launcher, env, _ = _launcher_harness(tmp_path, environment)
    result = _run(launcher, env=env)
    assert result.returncode == 2
    assert "invalid protected configuration" in result.stderr
    assert "value" not in result.stderr and "again" not in result.stderr


def test_launcher_rejects_unsupported_commands_and_unsafe_files(tmp_path: Path) -> None:
    launcher, env, _ = _launcher_harness(tmp_path, "MONITOR_API_TOKEN=value\n")
    assert _run(launcher, "shell", env=env).returncode == 2
    config = launcher.parent / "monitor-agent.env"
    config.unlink()
    config.symlink_to(tmp_path / "other.env")
    result = _run(launcher, env=env)
    assert result.returncode == 2
    assert "unavailable" in result.stderr


def test_installer_has_transactional_preflight_and_locked_installation() -> None:
    text = _script(INSTALLER)
    assert text.startswith("#!/bin/sh\nset -eu\n")
    for command in ("install", "stat", "chown", "chmod", "plutil", "launchctl"):
        assert f'command -v "{command}"' in text
    assert "${MONITOR_AGENT_PYTHON:-python3.11}" in text
    assert "CPython" in text
    for minor in ("11", "12", "13", "14"):
        assert minor in text
    assert '--require-hashes -r "$requirements_path"' in text
    assert '--no-deps --force-reinstall "$wheel_path"' in text
    assert "install --upgrade pip" not in text
    assert 'install_root="/Library/Application Support/MonitorAgent"' in text
    assert 'log_root="/Library/Logs/MonitorAgent"' in text
    assert 'plist_target="/Library/LaunchDaemons/com.company.monitor-agent.plist"' in text
    assert 'mktemp -d "$app_parent/.monitor-agent-transaction.' in text
    assert "rollback" in text.lower()
    assert 'launchctl stop "$launchd_label"' in text
    assert 'launchctl bootstrap system "$plist_target"' in text
    assert "launchctl enable system/com.company.monitor-agent" in text
    assert 'plutil -lint "$staged_plist"' in text
    assert '"$staged_root/run-agent.sh" check-config' in text
    assert "|| true" not in text
    assert '"$launchd_running" -ne 1' in text
    assert text.index('launchctl bootout "system/$launchd_label"') < text.index(
        'mv "$install_root/venv" "$backup_root/venv"'
    )
    assert text.index("stop_current_launchdaemon || rollback_failed=1") < text.index(
        "restore_component \\"
    )
    for component in ("venv", "launcher", "environment", "plist"):
        assert f"{component}_backup_completed=0" in text
        assert f"{component}_publication_completed=0" in text


def test_uninstaller_is_state_aware_and_preserves_data_by_default() -> None:
    text = _script(UNINSTALLER)
    assert text.startswith("#!/bin/sh\nset -eu\n")
    assert "expected no arguments or --purge" in text
    assert "launchctl bootout system/com.company.monitor-agent" in text
    assert "unable to inspect LaunchDaemon state" in text
    assert "unable to verify LaunchDaemon removal" in text
    assert 'rm -rf "$venv_dir"' in text
    assert 'rm -f "$launcher"' in text
    assert 'if [ "$purge" -eq 1 ]; then' in text
    assert 'rm -rf "$install_root"' in text
    assert 'rm -rf "$log_root"' in text
    assert "|| true" not in text


def test_environment_example_uses_literal_macos_paths_and_placeholder_secret() -> None:
    lines = ENV_EXAMPLE.read_text(encoding="utf-8").splitlines()
    assert "MONITOR_API_TOKEN=replace-with-managed-secret" in lines
    assert "MONITOR_SPOOL_PATH=/Library/Application Support/MonitorAgent/spool" in lines
    assert "MONITOR_LOG_PATH=/Library/Logs/MonitorAgent/monitor-agent.log" in lines
    assert "MONITOR_LOG_FORMAT=json" in lines
    assert not any(
        line.startswith("MONITOR_API_TOKEN=") and "replace" not in line for line in lines
    )
    assert all(
        not value.startswith(('"', "'")) for _, _, value in (line.partition("=") for line in lines)
    )


def test_shell_scripts_pass_posix_syntax_and_no_insecure_modes() -> None:
    result = subprocess.run(
        ["sh", "-n", str(LAUNCHER), str(INSTALLER), str(UNINSTALLER)],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    for path in (LAUNCHER, INSTALLER, UNINSTALLER):
        mode = stat.S_IMODE(path.stat().st_mode)
        assert mode & stat.S_IXUSR
        assert mode & 0o022 == 0


def test_installer_stage_failure_does_not_mutate_live_files(tmp_path: Path) -> None:
    installer, _, install_root, env, _ = _deployment_harness(tmp_path, loaded=True)
    (install_root / "venv").mkdir(parents=True)
    (install_root / "venv" / "old").write_text("old", encoding="utf-8")
    (install_root / "monitor-agent.env").write_text("MONITOR_API_TOKEN=old\n", encoding="utf-8")
    env["FAIL_STAGE"] = "plutil"
    result = _run(
        installer,
        tmp_path / "monitor_agent-2.0.0-py3-none-any.whl",
        tmp_path / "managed.env",
        env=env,
    )
    assert result.returncode != 0
    assert (install_root / "venv" / "old").read_text(encoding="utf-8") == "old"
    assert (install_root / "monitor-agent.env").read_text(
        encoding="utf-8"
    ) == "MONITOR_API_TOKEN=old\n"
    assert (tmp_path / "launchd-state").read_text(encoding="utf-8") == "1\n"
    assert not list(install_root.parent.glob(".monitor-agent-transaction.*"))


@pytest.mark.parametrize("failed_stage", ["bootstrap", "enable"])
def test_installer_activation_failure_rolls_back_prior_runtime_and_launchd(
    tmp_path: Path, failed_stage: str
) -> None:
    installer, _, install_root, env, _ = _deployment_harness(tmp_path, loaded=True)
    (install_root / "venv").mkdir(parents=True)
    (install_root / "venv" / "old").write_text("old", encoding="utf-8")
    (install_root / "run-agent.sh").write_text("old launcher", encoding="utf-8")
    (install_root / "monitor-agent.env").write_text("MONITOR_API_TOKEN=old\n", encoding="utf-8")
    plist = tmp_path / "managed" / "Library" / "LaunchDaemons" / "com.company.monitor-agent.plist"
    plist.parent.mkdir(parents=True, exist_ok=True)
    plist.write_text("old plist", encoding="utf-8")
    env["FAIL_STAGE"] = failed_stage
    result = _run(
        installer,
        tmp_path / "monitor_agent-2.0.0-py3-none-any.whl",
        tmp_path / "managed.env",
        env=env,
    )
    assert result.returncode != 0
    assert (install_root / "venv" / "old").read_text(encoding="utf-8") == "old"
    assert (install_root / "run-agent.sh").read_text(encoding="utf-8") == "old launcher"
    assert plist.read_text(encoding="utf-8") == "old plist"
    assert (tmp_path / "launchd-state").read_text(encoding="utf-8") == "1\n"


@pytest.mark.parametrize("failed_backup", ["run-agent.sh", "plist"])
def test_installer_backup_rename_failure_preserves_every_prior_artifact_and_daemon(
    tmp_path: Path, failed_backup: str
) -> None:
    installer, _, install_root, env, _ = _deployment_harness(tmp_path, loaded=True)
    (install_root / "venv").mkdir(parents=True)
    (install_root / "venv" / "old").write_text("old venv", encoding="utf-8")
    (install_root / "run-agent.sh").write_text("old launcher", encoding="utf-8")
    (install_root / "monitor-agent.env").write_text(
        "MONITOR_API_TOKEN=old secret\n", encoding="utf-8"
    )
    plist = tmp_path / "managed" / "Library" / "LaunchDaemons" / "com.company.monitor-agent.plist"
    plist.write_text("old plist", encoding="utf-8")
    env["FAIL_MV_SOURCE"] = str(
        install_root / "run-agent.sh" if failed_backup == "run-agent.sh" else plist
    )

    result = _run(
        installer,
        tmp_path / "monitor_agent-2.0.0-py3-none-any.whl",
        tmp_path / "managed.env",
        env=env,
    )

    assert result.returncode != 0
    assert (install_root / "venv" / "old").read_text(encoding="utf-8") == "old venv"
    assert (install_root / "run-agent.sh").read_text(encoding="utf-8") == "old launcher"
    assert (install_root / "monitor-agent.env").read_text(
        encoding="utf-8"
    ) == "MONITOR_API_TOKEN=old secret\n"
    assert plist.read_text(encoding="utf-8") == "old plist"
    assert (tmp_path / "launchd-state").read_text(encoding="utf-8") == "1\n"
    assert (tmp_path / "launchd-running").read_text(encoding="utf-8") == "1\n"


def test_installer_restores_a_prior_loaded_but_stopped_daemon(tmp_path: Path) -> None:
    installer, _, install_root, env, _ = _deployment_harness(tmp_path, loaded=True)
    (tmp_path / "launchd-running").write_text("0\n", encoding="utf-8")
    (install_root / "venv").mkdir(parents=True)
    plist = tmp_path / "managed" / "Library" / "LaunchDaemons" / "com.company.monitor-agent.plist"
    plist.write_text("old plist", encoding="utf-8")
    env["FAIL_STAGE"] = "enable"
    result = _run(
        installer,
        tmp_path / "monitor_agent-2.0.0-py3-none-any.whl",
        tmp_path / "managed.env",
        env=env,
    )
    assert result.returncode != 0
    assert (tmp_path / "launchd-state").read_text(encoding="utf-8") == "1\n"
    assert (tmp_path / "launchd-running").read_text(encoding="utf-8") == "0\n"


def test_installer_rejects_a_symlinked_live_root_before_launchd_mutation(
    tmp_path: Path,
) -> None:
    installer, _, install_root, env, _ = _deployment_harness(tmp_path, loaded=True)
    destination = tmp_path / "outside"
    destination.mkdir()
    install_root.symlink_to(destination, target_is_directory=True)
    result = _run(
        installer,
        tmp_path / "monitor_agent-2.0.0-py3-none-any.whl",
        tmp_path / "managed.env",
        env=env,
    )
    assert result.returncode != 0
    assert (tmp_path / "launchd-state").read_text(encoding="utf-8") == "1\n"
    assert not any(destination.iterdir())


def test_installer_rejects_loaded_daemon_without_a_managed_plist(tmp_path: Path) -> None:
    installer, _, install_root, env, _ = _deployment_harness(tmp_path, loaded=True)
    result = _run(
        installer,
        tmp_path / "monitor_agent-2.0.0-py3-none-any.whl",
        tmp_path / "managed.env",
        env=env,
    )
    assert result.returncode != 0
    assert (tmp_path / "launchd-state").read_text(encoding="utf-8") == "1\n"
    assert (tmp_path / "launchd-running").read_text(encoding="utf-8") == "1\n"
    assert not install_root.exists()
    assert not list(install_root.parent.glob(".monitor-agent-transaction.*"))


def test_installer_successful_upgrade_replaces_runtime_preserves_data_and_cleans_transaction(
    tmp_path: Path,
) -> None:
    installer, _, install_root, env, log_root = _deployment_harness(tmp_path, loaded=True)
    (install_root / "venv").mkdir(parents=True)
    (install_root / "venv" / "old").write_text("old venv", encoding="utf-8")
    (install_root / "run-agent.sh").write_text("old launcher", encoding="utf-8")
    (install_root / "monitor-agent.env").write_text(
        "MONITOR_API_TOKEN=old secret\n", encoding="utf-8"
    )
    plist = tmp_path / "managed" / "Library" / "LaunchDaemons" / "com.company.monitor-agent.plist"
    plist.write_text("old plist", encoding="utf-8")
    spool = install_root / "spool"
    spool.mkdir()
    (spool / "queued.json").write_text("queued", encoding="utf-8")
    log_root.mkdir(parents=True)
    (log_root / "agent.log").write_text("prior log", encoding="utf-8")
    result = _run(
        installer,
        tmp_path / "monitor_agent-2.0.0-py3-none-any.whl",
        tmp_path / "managed.env",
        env=env,
    )
    assert result.returncode == 0, result.stderr
    assert (spool / "queued.json").read_text(encoding="utf-8") == "queued"
    assert (log_root / "agent.log").read_text(encoding="utf-8") == "prior log"
    assert (install_root / "venv" / "bin" / "monitor-agent").is_file()
    assert not (install_root / "venv" / "old").exists()
    assert (install_root / "run-agent.sh").read_text(encoding="utf-8") == _script(LAUNCHER)
    assert (install_root / "monitor-agent.env").read_text(
        encoding="utf-8"
    ) == "MONITOR_API_TOKEN=new secret=value\n"
    assert plist.read_bytes() == PLIST.read_bytes()
    assert (tmp_path / "launchd-state").read_text(encoding="utf-8") == "1\n"
    assert (tmp_path / "launchd-running").read_text(encoding="utf-8") == "1\n"
    assert not list(install_root.parent.glob(".monitor-agent-transaction.*"))


def test_installer_success_cleanup_failure_names_retained_recovery(
    tmp_path: Path,
) -> None:
    installer, _, install_root, env, _ = _deployment_harness(tmp_path)
    env["FAIL_RM_TRANSACTION"] = "1"

    result = _run(
        installer,
        tmp_path / "monitor_agent-2.0.0-py3-none-any.whl",
        tmp_path / "managed.env",
        env=env,
    )

    transactions = list(install_root.parent.glob(".monitor-agent-transaction.*"))
    assert result.returncode != 0
    assert len(transactions) == 1
    assert result.stderr == (
        f"monitor-agent install: cleanup incomplete; recovery retained at {transactions[0]}\n"
    )
    assert "new secret=value" not in result.stderr


def test_uninstaller_handles_absence_unexpected_state_default_and_purge(tmp_path: Path) -> None:
    _, uninstaller, install_root, env, log_root = _deployment_harness(tmp_path)
    (install_root / "venv").mkdir(parents=True)
    (install_root / "run-agent.sh").write_text("launcher", encoding="utf-8")
    (install_root / "monitor-agent.env").write_text("MONITOR_API_TOKEN=keep\n", encoding="utf-8")
    (install_root / "spool").mkdir()
    (install_root / "spool" / "queued").write_text("keep", encoding="utf-8")
    log_root.mkdir(parents=True)
    (log_root / "agent.log").write_text("keep", encoding="utf-8")
    result = _run(uninstaller, env=env)
    assert result.returncode == 0, result.stderr
    assert (install_root / "monitor-agent.env").is_file()
    assert (install_root / "spool" / "queued").is_file()
    assert (log_root / "agent.log").is_file()
    assert not (install_root / "venv").exists()
    assert not (install_root / "run-agent.sh").exists()
    env["LAUNCHD_UNEXPECTED"] = "1"
    blocked = _run(uninstaller, env=env)
    assert blocked.returncode == 2
    del env["LAUNCHD_UNEXPECTED"]
    purged = _run(uninstaller, "--purge", env=env)
    assert purged.returncode == 0, purged.stderr
    assert not install_root.exists()
    assert not log_root.exists()
