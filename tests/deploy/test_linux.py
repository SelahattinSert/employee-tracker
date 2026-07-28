from __future__ import annotations

import os
import re
import shlex
import shutil
import socket
import stat
import subprocess
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
DEPLOY = ROOT / "deploy" / "linux"
SERVICE = DEPLOY / "monitor-agent.service"
INSTALLER = DEPLOY / "install.sh"
UNINSTALLER = DEPLOY / "uninstall.sh"
ENV_EXAMPLE = DEPLOY / "monitor-agent.env.example"

EXPECTED_UNIT = {
    "Unit": [
        "Description=Monitor Agent 2.0 Endpoint Telemetry",
        "Documentation=file:/opt/monitor-agent/README.md",
        "After=network-online.target",
        "Wants=network-online.target",
    ],
    "Service": [
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
    ],
    "Install": ["WantedBy=multi-user.target"],
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

FAILURE_STAGES = [
    "venv",
    "hash-install",
    "wheel-install",
    "environment-stage",
    "environment-stage-partial",
    "unit-stage",
    "unit-stage-partial",
    "runtime-swap",
    "environment-rename",
    "unit-rename",
    "daemon-reload",
    "enable",
    "restart",
    "active-verify",
    "after-enable",
    "after-restart",
]

MANAGED_DIRECTORY_FAILURE_STAGES = [
    *(f"directory-create-{index}" for index in range(8)),
    "config-mode",
    "state-mode",
]

ALLOWED_RECURSIVE_DELETIONS = [
    ("rm", "-rf", "--", "/opt/monitor-agent"),
    ("rm", "-rf", "--", "/etc/monitor-agent", "/var/lib/monitor-agent"),
]

ALLOWED_UNINSTALLER_COMMAND_SUBSTITUTIONS = {
    "if active_state=$(systemctl is-active -- monitor-agent.service 2>/dev/null); then",
    "if enabled_state=$(systemctl is-enabled -- monitor-agent.service 2>/dev/null); then",
}


def _replace_once(text: str, expected: str, replacement: str) -> str:
    assert text.count(expected) == 1, expected
    return text.replace(expected, replacement)


def _write_test_installer(source: Path, destination: Path, stage: Path) -> None:
    """Copy the production installer and narrowly redirect literal paths for tests."""
    text = source.read_text(encoding="utf-8")
    text = _replace_once(
        text,
        'if [ "$EUID" -ne 0 ]; then',
        'if [ "$EUID" -ne 0 ] && [ "${MONITOR_AGENT_TEST_BYPASS_ROOT:-}" != 1 ]; then',
    )
    replacements = {
        "install_dir=/opt/monitor-agent": f"install_dir={stage}/opt/monitor-agent",
        "opt_parent=/opt": f"opt_parent={stage}/opt",
        "etc_parent=/etc": f"etc_parent={stage}/etc",
        "config_dir=/etc/monitor-agent": f"config_dir={stage}/etc/monitor-agent",
        "etc_systemd_dir=/etc/systemd": f"etc_systemd_dir={stage}/etc/systemd",
        "unit_dir=/etc/systemd/system": f"unit_dir={stage}/etc/systemd/system",
        "config_file=/etc/monitor-agent/monitor-agent.env": (
            f"config_file={stage}/etc/monitor-agent/monitor-agent.env"
        ),
        "var_parent=/var": f"var_parent={stage}/var",
        "var_lib_dir=/var/lib": f"var_lib_dir={stage}/var/lib",
        "state_dir=/var/lib/monitor-agent": f"state_dir={stage}/var/lib/monitor-agent",
        "unit_file=/etc/systemd/system/monitor-agent.service": (
            f"unit_file={stage}/etc/systemd/system/monitor-agent.service"
        ),
    }
    for expected, replacement in replacements.items():
        text = _replace_once(text, expected, replacement)
    destination.write_text(text, encoding="utf-8")
    destination.chmod(0o755)


def _write_test_uninstaller(source: Path, destination: Path, stage: Path) -> None:
    """Copy the production uninstaller and redirect only its literal targets."""
    text = source.read_text(encoding="utf-8")
    text = _replace_once(
        text,
        'if [ "$EUID" -ne 0 ]; then',
        'if [ "$EUID" -ne 0 ] && [ "${MONITOR_AGENT_TEST_BYPASS_ROOT:-}" != 1 ]; then',
    )
    replacements = {
        "rm -rf -- /opt/monitor-agent": f"rm -rf -- {stage}/opt/monitor-agent",
        "rm -f -- /etc/systemd/system/monitor-agent.service": (
            f"rm -f -- {stage}/etc/systemd/system/monitor-agent.service"
        ),
        "rm -rf -- /etc/monitor-agent /var/lib/monitor-agent": (
            f"rm -rf -- {stage}/etc/monitor-agent {stage}/var/lib/monitor-agent"
        ),
    }
    for expected, replacement in replacements.items():
        text = _replace_once(text, expected, replacement)
    destination.write_text(text, encoding="utf-8")
    destination.chmod(0o755)


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def _run(
    script: Path,
    *arguments: Path | str,
    env: dict[str, str],
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(script), *(str(argument) for argument in arguments)],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )


def _parse_unit() -> dict[str, list[str]]:
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for raw_line in SERVICE.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            current = line[1:-1]
            assert current not in sections
            sections[current] = []
            continue
        assert current is not None
        sections[current].append(line)
    return sections


def _snapshot(root: Path) -> dict[str, tuple[str, int, bytes | str | None]]:
    snapshot: dict[str, tuple[str, int, bytes | str | None]] = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            snapshot[relative] = ("symlink", 0, os.readlink(path))
        elif path.is_dir():
            snapshot[relative] = ("directory", stat.S_IMODE(path.stat().st_mode), None)
        elif stat.S_ISREG(path.lstat().st_mode):
            snapshot[relative] = (
                "file",
                stat.S_IMODE(path.stat().st_mode),
                path.read_bytes(),
            )
        else:
            snapshot[relative] = (
                "special",
                stat.S_IMODE(path.lstat().st_mode),
                stat.S_IFMT(path.lstat().st_mode).to_bytes(4, "little"),
            )
    return snapshot


def _state(path: Path) -> tuple[int, int]:
    values = {
        key: int(value)
        for key, value in (
            line.split("=", maxsplit=1)
            for line in path.read_text(encoding="utf-8").splitlines()
        )
    }
    return values["active"], values["enabled"]


def _assert_recursive_deletion_policy(text: str) -> None:
    if "\\\n" in text:
        raise ValueError("continued command")

    recursive: list[tuple[str, ...]] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "`" in line:
            raise ValueError("invalid shell")
        if "$(" in line and line not in ALLOWED_UNINSTALLER_COMMAND_SUBSTITUTIONS:
            raise ValueError("invalid shell")
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", line):
            _, _, assigned_value = line.partition("=")
            try:
                stored_tokens = shlex.split(assigned_value, posix=True)
            except ValueError as error:
                raise ValueError("invalid shell") from error
            stored_words = [
                word for token in stored_tokens for word in shlex.split(token, posix=True)
            ]
            if any(word == "rm" or word.endswith("/rm") for word in stored_words):
                raise ValueError("stored command")
        try:
            tokens = shlex.split(line, posix=True)
        except ValueError as error:
            raise ValueError("invalid shell") from error
        if not tokens:
            continue
        if tokens[0].startswith(("$", "${")):
            raise ValueError("variable command")
        rm_positions = [
            index
            for index, token in enumerate(tokens)
            if token == "rm" or token.endswith("/rm")
        ]
        if not rm_positions:
            continue
        rm_index = rm_positions[0]
        arguments = tokens[rm_index + 1 :]
        short_options = "".join(
            option[1:]
            for option in arguments
            if option.startswith("-") and not option.startswith("--")
        )
        recursive_option = (
            "r" in short_options or "R" in short_options or "--recursive" in arguments
        )
        if not recursive_option:
            continue
        if any(any(character in token for character in "*?[") for token in tokens):
            raise ValueError("glob")
        command = tuple(tokens)
        if rm_index != 0 or command not in ALLOWED_RECURSIVE_DELETIONS:
            raise ValueError("unsafe recursive deletion")
        recursive.append(command)

    if recursive != ALLOWED_RECURSIVE_DELETIONS:
        raise ValueError("recursive deletion set changed")


class Harness:
    def __init__(
        self,
        tmp_path: Path,
        *,
        existing: bool = False,
        active: bool = False,
        enabled: bool = False,
        readme: bool = False,
    ) -> None:
        self.tmp_path = tmp_path
        self.project = tmp_path / "project"
        self.linux = self.project / "deploy" / "linux"
        self.linux.mkdir(parents=True)
        shutil.copy2(SERVICE, self.linux / "monitor-agent.service")
        shutil.copy2(ROOT / "requirements.lock", self.project / "requirements.lock")
        if readme:
            (self.project / "README.md").write_text("deployment guide\n", encoding="utf-8")

        self.stage = tmp_path / "stage"
        self.stage.mkdir(mode=0o700)
        _write_test_installer(INSTALLER, self.linux / "install.sh", self.stage)
        _write_test_uninstaller(UNINSTALLER, self.linux / "uninstall.sh", self.stage)
        self.wheel = tmp_path / "monitor_agent-2.0.0-py3-none-any.whl"
        self.wheel.write_bytes(b"wheel fixture")
        self.environment = tmp_path / "supplied.env"
        self.environment.write_text(
            "MONITOR_API_TOKEN=top-secret-test-token\n"
            "MONITOR_SPOOL_PATH=/var/lib/monitor-agent/spool\n",
            encoding="utf-8",
        )
        self.environment.chmod(0o640)

        self.log = tmp_path / "commands.log"
        self.log.write_text("", encoding="utf-8")
        self.service_state = tmp_path / "service.state"
        self.failure_marker = tmp_path / "failure.marker"
        self.fake_bin = tmp_path / "fake-bin"
        self.fake_bin.mkdir()
        self._write_state(active=active, enabled=enabled)
        self._write_fakes()
        if existing:
            self._write_existing_install()

    @property
    def runtime(self) -> Path:
        return self.stage / "opt" / "monitor-agent"

    @property
    def config(self) -> Path:
        return self.stage / "etc" / "monitor-agent" / "monitor-agent.env"

    @property
    def state_dir(self) -> Path:
        return self.stage / "var" / "lib" / "monitor-agent"

    @property
    def unit(self) -> Path:
        return self.stage / "etc" / "systemd" / "system" / "monitor-agent.service"

    @property
    def managed_directories(self) -> list[Path]:
        return [
            self.stage / "opt",
            self.stage / "etc",
            self.config.parent,
            self.stage / "etc/systemd",
            self.unit.parent,
            self.stage / "var",
            self.stage / "var/lib",
            self.state_dir,
        ]

    def _write_state(self, *, active: bool, enabled: bool) -> None:
        self.service_state.write_text(
            f"active={int(active)}\n"
            f"enabled={int(enabled)}\n"
            "active_queries=0\n"
            "enabled_queries=0\n",
            encoding="utf-8",
        )

    def _write_existing_install(self) -> None:
        directories = [
            (self.stage / "opt", 0o711),
            (self.runtime, 0o750),
            (self.stage / "etc", 0o751),
            (self.config.parent, 0o750),
            (self.stage / "etc/systemd", 0o752),
            (self.unit.parent, 0o753),
            (self.stage / "var", 0o754),
            (self.stage / "var/lib", 0o755),
            (self.state_dir, 0o750),
        ]
        for directory, mode in directories:
            directory.mkdir(exist_ok=True)
            directory.chmod(mode)
        (self.runtime / "old-runtime").write_text("keep runtime\n", encoding="utf-8")
        self.config.write_text("MONITOR_API_TOKEN=old-secret\n", encoding="utf-8")
        self.config.chmod(0o640)
        self.unit.write_text("old unit\n", encoding="utf-8")
        self.unit.chmod(0o600)
        (self.state_dir / "telemetry.json").write_text("keep state\n", encoding="utf-8")

    def _write_fakes(self) -> None:
        _write_executable(
            self.fake_bin / "python3.11",
            """#!/usr/bin/env bash
set -eu
printf 'python' >> "$FAKE_COMMAND_LOG"
printf ' <%s>' "$@" >> "$FAKE_COMMAND_LOG"
printf '\n' >> "$FAKE_COMMAND_LOG"
fail_once() {
    if [ "${FAKE_FAIL_STAGE:-}" = "$1" ] && [ ! -e "$FAKE_FAILURE_MARKER" ]; then
        : > "$FAKE_FAILURE_MARKER"
        return 0
    fi
    return 1
}
if [ "${1-}" = "-c" ]; then
    printf '%s %s %s\n' \
        "${FAKE_IMPLEMENTATION:-CPython}" "${FAKE_MAJOR:-3}" "${FAKE_MINOR:-11}"
elif [ "${1-}" = "-m" ] && [ "${2-}" = "venv" ]; then
    if fail_once venv; then
        exit 71
    fi
    target="${3:?}"
    mkdir -p -- "$target/bin"
    cp -- "$0" "$target/bin/python"
elif [ "${1-}" = "-m" ] && [ "${2-}" = "pip" ]; then
    if [[ " $* " = *" --require-hashes "* ]]; then
        if fail_once hash-install; then
            exit 72
        fi
    elif fail_once wheel-install; then
        exit 73
    fi
fi
""",
        )
        _write_executable(
            self.fake_bin / "install",
            """#!/usr/bin/env bash
set -eu
printf 'install' >> "$FAKE_COMMAND_LOG"
printf ' <%s>' "$@" >> "$FAKE_COMMAND_LOG"
printf '\n' >> "$FAKE_COMMAND_LOG"
destination="${!#}"
stage=
if [ "${FAKE_FAIL_STAGE:-}" = "directory-create" ] \
    && [ "$destination" = "$FAKE_DIRECTORY_FAIL_TARGET" ]; then
    : > "$FAKE_FAILURE_MARKER"
    exit 74
fi
if [[ " $* " = *" $FAKE_ENV_INPUT "* ]] \
    && [[ "$(basename -- "$destination")" = .monitor-agent.env.* ]]; then
    stage=environment-stage
elif [[ " $* " = *" $FAKE_SERVICE_INPUT "* ]] \
    && [[ "$(basename -- "$destination")" = .monitor-agent.service.* ]]; then
    stage=unit-stage
fi
if [ "${FAKE_FAIL_STAGE:-}" = "any-install" ]; then
    exit 74
fi
if [ -n "$stage" ] && [ "${FAKE_FAIL_STAGE:-}" = "$stage-partial" ]; then
    /usr/bin/install "$@"
    printf 'partial\n' > "$destination"
    : > "$FAKE_FAILURE_MARKER"
    exit 75
fi
if [ -n "$stage" ] && [ "${FAKE_FAIL_STAGE:-}" = "$stage" ]; then
    : > "$FAKE_FAILURE_MARKER"
    exit 76
fi
exec /usr/bin/install "$@"
""",
        )
        _write_executable(
            self.fake_bin / "chmod",
            """#!/usr/bin/env bash
set -eu
printf 'chmod' >> "$FAKE_COMMAND_LOG"
printf ' <%s>' "$@" >> "$FAKE_COMMAND_LOG"
printf '\n' >> "$FAKE_COMMAND_LOG"
destination="${!#}"
if [ "${FAKE_FAIL_STAGE:-}" = "directory-mode" ] \
    && [[ " $* " = *" $FAKE_MODE_FAIL_TARGET "* ]] \
    && [ ! -e "$FAKE_FAILURE_MARKER" ]; then
    : > "$FAKE_FAILURE_MARKER"
    exit 86
fi
if [ "${FAKE_RESTORE_FAILURE:-}" = "directory-mode" ] \
    && [ "$destination" = "$FAKE_STATE_DIR" ] \
    && [ -e "$FAKE_FAILURE_MARKER" ] \
    && [ ! -e "$FAKE_RESTORE_MARKER" ]; then
    : > "$FAKE_RESTORE_MARKER"
    if [ "${FAKE_NATIVE_NOISE:-0}" = 1 ]; then
        printf 'native stdout %s %s\n' "$FAKE_ROLLBACK_NOISE_PATH" "$FAKE_NATIVE_SENTINEL"
        printf 'native stderr %s %s\n' "$FAKE_ROLLBACK_NOISE_PATH" "$FAKE_NATIVE_SENTINEL" >&2
    fi
    exit 87
fi
exec /usr/bin/chmod "$@"
""",
        )
        _write_executable(
            self.fake_bin / "mv",
            """#!/usr/bin/env bash
set -eu
printf 'mv' >> "$FAKE_COMMAND_LOG"
printf ' <%s>' "$@" >> "$FAKE_COMMAND_LOG"
printf '\n' >> "$FAKE_COMMAND_LOG"
destination="${!#}"
source="${@: -2:1}"
emit_native_failure() {
    if [ "${FAKE_NATIVE_NOISE:-0}" != 1 ]; then
        return
    fi
    printf 'native stdout %s %s\n' "$FAKE_ROLLBACK_NOISE_PATH" "$FAKE_NATIVE_SENTINEL"
    printf 'native stderr %s %s\n' "$FAKE_ROLLBACK_NOISE_PATH" "$FAKE_NATIVE_SENTINEL" >&2
}
stage=
if [[ "$destination" = */opt/monitor-agent || "$destination" = */monitor-agent ]] \
    && [ "$(basename -- "$source")" = "runtime" ]; then
    stage=runtime-swap
elif [[ "$destination" = */monitor-agent.env ]] \
    && [[ "$(basename -- "$source")" = .monitor-agent.env.* ]]; then
    stage=environment-rename
elif [[ "$destination" = */monitor-agent.service ]] \
    && [[ "$(basename -- "$source")" = .monitor-agent.service.* ]]; then
    stage=unit-rename
fi
swap_path() {
    if [ -n "$stage" ] \
        && [ -n "${FAKE_SWAP_TRIGGER:-}" ] \
        && [ "${FAKE_SWAP_TRIGGER:-}" = "$stage" ] \
        && [ ! -e "$FAKE_SWAP_MARKER" ]; then
        /usr/bin/mv -- "$FAKE_SWAP_PATH" "$FAKE_SWAP_PATH.detached"
        ln -s -- "$FAKE_SWAP_TARGET" "$FAKE_SWAP_PATH"
        : > "$FAKE_SWAP_MARKER"
    fi
}
swap_path
if [ -n "$stage" ] \
    && [ -n "${FAKE_PUBLICATION_SWAP:-}" ] \
    && [ "${FAKE_PUBLICATION_SWAP:-}" = "$stage" ] \
    && [ ! -e "$FAKE_SWAP_MARKER" ]; then
    /usr/bin/rm -rf -- "$destination"
    mkdir -- "$destination"
    : > "$FAKE_SWAP_MARKER"
fi
if [ -n "$stage" ] && [ "${FAKE_FAIL_STAGE:-}" = "$stage" ] \
    && [ ! -e "$FAKE_FAILURE_MARKER" ]; then
    : > "$FAKE_FAILURE_MARKER"
    exit 77
fi
case "${FAKE_RESTORE_FAILURE:-}" in
    runtime)
        if [[ "$source" = */previous-runtime ]] && [ ! -e "$FAKE_RESTORE_MARKER" ]; then
            : > "$FAKE_RESTORE_MARKER"
            emit_native_failure
            exit 88
        fi
        ;;
    environment)
        if [[ "$source" = */.monitor-agent.env.backup.* ]] && [ ! -e "$FAKE_RESTORE_MARKER" ]; then
            : > "$FAKE_RESTORE_MARKER"
            emit_native_failure
            exit 89
        fi
        ;;
    unit)
        if [[ "$source" = */.monitor-agent.service.backup.* ]] \
            && [ ! -e "$FAKE_RESTORE_MARKER" ]; then
            : > "$FAKE_RESTORE_MARKER"
            emit_native_failure
            exit 89
        fi
        ;;
esac
exec /usr/bin/mv "$@"
""",
        )
        _write_executable(
            self.fake_bin / "rm",
            """#!/usr/bin/env bash
set -eu
printf 'rm' >> "$FAKE_COMMAND_LOG"
printf ' <%s>' "$@" >> "$FAKE_COMMAND_LOG"
printf '\n' >> "$FAKE_COMMAND_LOG"
cleanup_stage=
emit_native_failure() {
    if [ "${FAKE_NATIVE_NOISE:-0}" != 1 ]; then
        return
    fi
    printf 'native stdout %s %s\n' "$FAKE_ROLLBACK_NOISE_PATH" "$FAKE_NATIVE_SENTINEL"
    printf 'native stderr %s %s\n' "$FAKE_ROLLBACK_NOISE_PATH" "$FAKE_NATIVE_SENTINEL" >&2
}
case " $* " in
    *".monitor-agent.env.backup."*)
        cleanup_stage=cleanup-environment-backup
        ;;
    *".monitor-agent.service.backup."*)
        cleanup_stage=cleanup-unit-backup
        ;;
    *".monitor-agent-install."*)
        cleanup_stage=cleanup-transaction
        ;;
esac
if [ -n "$cleanup_stage" ] && [ "${FAKE_FAIL_STAGE:-}" = "$cleanup_stage" ] \
    && [ ! -e "$FAKE_FAILURE_MARKER" ]; then
    : > "$FAKE_FAILURE_MARKER"
    emit_native_failure
    exit 90
fi
if [ "${FAKE_RESTORE_FAILURE:-}" = "artifact-removal" ] \
    && [ "$cleanup_stage" = "cleanup-transaction" ] \
    && [ -e "$FAKE_FAILURE_MARKER" ] \
    && [ ! -e "$FAKE_RESTORE_MARKER" ]; then
    : > "$FAKE_RESTORE_MARKER"
    emit_native_failure
    exit 91
fi
if [ "${FAKE_SWAP_TRIGGER:-}" = "success-cleanup" ] \
    && [ "$cleanup_stage" = "cleanup-transaction" ] \
    && [ ! -e "$FAKE_SWAP_MARKER" ]; then
    /usr/bin/mv -- "$FAKE_SWAP_PATH" "$FAKE_SWAP_PATH.detached"
    ln -s -- "$FAKE_SWAP_TARGET" "$FAKE_SWAP_PATH"
    : > "$FAKE_SWAP_MARKER"
fi
if [ "${FAKE_SWAP_TRIGGER:-}" = "rollback-delete" ] \
    && [[ " $* " = *"/monitor-agent "* ]] \
    && [ -e "$FAKE_FAILURE_MARKER" ] \
    && [ ! -e "$FAKE_SWAP_MARKER" ]; then
    /usr/bin/mv -- "$FAKE_SWAP_PATH" "$FAKE_SWAP_PATH.detached"
    ln -s -- "$FAKE_SWAP_TARGET" "$FAKE_SWAP_PATH"
    : > "$FAKE_SWAP_MARKER"
fi
case " $* " in
    *" /opt/monitor-agent "* | *" /etc/monitor-agent "* | \
        *" /var/lib/monitor-agent "* | *" /etc/systemd/system/monitor-agent.service "*)
        exit 0
        ;;
esac
exec /usr/bin/rm "$@"
""",
        )
        _write_executable(
            self.fake_bin / "rmdir",
            """#!/usr/bin/env bash
set -eu
printf 'rmdir' >> "$FAKE_COMMAND_LOG"
printf ' <%s>' "$@" >> "$FAKE_COMMAND_LOG"
printf '\n' >> "$FAKE_COMMAND_LOG"
destination="${!#}"
if [ "${FAKE_RESTORE_FAILURE:-}" = "directory-removal" ] \
    && [ "$destination" = "$FAKE_DIRECTORY_REMOVAL_TARGET" ] \
    && [ -e "$FAKE_FAILURE_MARKER" ] \
    && [ ! -e "$FAKE_RESTORE_MARKER" ]; then
    : > "$FAKE_RESTORE_MARKER"
    if [ "${FAKE_NATIVE_NOISE:-0}" = 1 ]; then
        printf 'native stdout %s %s\\n' "$FAKE_ROLLBACK_NOISE_PATH" "$FAKE_NATIVE_SENTINEL"
        printf 'native stderr %s %s\\n' "$FAKE_ROLLBACK_NOISE_PATH" "$FAKE_NATIVE_SENTINEL" >&2
    fi
    exit 92
fi
exec /usr/bin/rmdir "$@"
""",
        )
        _write_executable(
            self.fake_bin / "systemctl",
            """#!/usr/bin/env bash
set -eu
printf 'systemctl' >> "$FAKE_COMMAND_LOG"
printf ' <%s>' "$@" >> "$FAKE_COMMAND_LOG"
printf '\n' >> "$FAKE_COMMAND_LOG"
. "$FAKE_SERVICE_STATE"
save() {
    printf 'active=%s\nenabled=%s\nactive_queries=%s\nenabled_queries=%s\n' \
        "$active" "$enabled" "$active_queries" "$enabled_queries" \
        > "$FAKE_SERVICE_STATE"
}
fail_once() {
    if [ "${FAKE_FAIL_STAGE:-}" = "$1" ] && [ ! -e "$FAKE_FAILURE_MARKER" ]; then
        : > "$FAKE_FAILURE_MARKER"
        return 0
    fi
    return 1
}
emit_native_failure() {
    if [ "${FAKE_NATIVE_NOISE:-0}" != 1 ]; then
        return
    fi
    printf 'native stdout %s %s\n' "$FAKE_ROLLBACK_NOISE_PATH" "$FAKE_NATIVE_SENTINEL"
    printf 'native stderr %s %s\n' "$FAKE_ROLLBACK_NOISE_PATH" "$FAKE_NATIVE_SENTINEL" >&2
}
swap_path() {
    if [ "${FAKE_SWAP_TRIGGER:-}" = "service-state" ] \
        && [ "$active_queries" -eq 0 ] \
        && [ ! -e "$FAKE_SWAP_MARKER" ]; then
        /usr/bin/mv -- "$FAKE_SWAP_PATH" "$FAKE_SWAP_PATH.detached"
        ln -s -- "$FAKE_SWAP_TARGET" "$FAKE_SWAP_PATH"
        : > "$FAKE_SWAP_MARKER"
    fi
}
unit_file_present=0
if [ -e "$FAKE_UNIT_FILE" ]; then
    unit_file_present=1
fi
present=$unit_file_present
if [ "$active" -eq 1 ] || [ "$enabled" -eq 1 ]; then
    present=1
fi
case "${1-}" in
    is-active)
        swap_path
        active_queries=$((active_queries + 1))
        save
        if [ "${FAKE_FAIL_STAGE:-}" = "uninstall-dbus-failure" ]; then
            exit 5
        fi
        if [ "${FAKE_FAIL_STAGE:-}" = "unexpected-active-state" ] \
            || [ "${FAKE_FAIL_STAGE:-}" = "uninstall-failed-state" ]; then
            printf 'failed\n'
            exit 3
        fi
        if { [ "${FAKE_FAIL_STAGE:-}" = "service-state" ] && [ "$active_queries" -eq 1 ]; } \
            || { { [ "${FAKE_FAIL_STAGE:-}" = "active-verify" ] \
                    || [ "${FAKE_FAIL_STAGE:-}" = "after-restart" ] \
                    || [ "${FAKE_FAIL_STAGE:-}" = "rollback-stop" ]; } \
                && [ "$active_queries" -ge 2 ] \
                && [ ! -e "$FAKE_FAILURE_MARKER" ]; }; then
            : > "$FAKE_FAILURE_MARKER"
            exit 5
        fi
        if [ "$present" -eq 0 ]; then
            printf 'not-found\n'
            exit 4
        fi
        if [ "$active" -eq 1 ]; then
            printf 'active\n'
            exit 0
        fi
        printf 'inactive\n'
        exit 3
        ;;
    is-enabled)
        enabled_queries=$((enabled_queries + 1))
        save
        if [ "${FAKE_FAIL_STAGE:-}" = "enabled-state" ] \
            && [ "$enabled_queries" -eq 1 ]; then
            exit 5
        fi
        if [ "${FAKE_FAIL_STAGE:-}" = "unexpected-enabled-state" ] \
            || [ "${FAKE_FAIL_STAGE:-}" = "uninstall-masked-state" ]; then
            printf 'masked\n'
            exit 1
        fi
        if [ "$present" -eq 0 ]; then
            printf 'not-found\n'
            exit 4
        fi
        if [ "$enabled" -eq 1 ]; then
            printf 'enabled\n'
            exit 0
        fi
        printf 'disabled\n'
        exit 1
        ;;
    daemon-reload)
        if fail_once daemon-reload; then
            exit 78
        fi
        exit 0
        ;;
    enable)
        fail_once enable && exit 79
        enabled=1
        save
        ;;
    restart | start)
        if fail_once restart || fail_once after-enable; then
            exit 80
        fi
        if [ "${FAKE_FAIL_STAGE:-}" = "rollback-start" ] \
            && [ -e "$FAKE_FAILURE_MARKER" ]; then
            exit 81
        fi
        active=1
        save
        ;;
    stop)
        if { [ "${FAKE_FAIL_STAGE:-}" = "rollback-stop" ] \
                || [ "${FAKE_RESTORE_FAILURE:-}" = "service" ]; } \
            && [ -e "$FAKE_FAILURE_MARKER" ]; then
            : > "$FAKE_RESTORE_MARKER"
            emit_native_failure
            exit 82
        fi
        active=0
        save
        ;;
    disable)
        if [[ " $* " = *" --now "* ]]; then
            case "${FAKE_FAIL_STAGE:-}" in
                uninstall-stop-failure)
                    enabled=0
                    save
                    exit 83
                    ;;
                uninstall-disable-failure)
                    active=0
                    save
                    exit 84
                    ;;
                uninstall-dbus-failure)
                    exit 5
                    ;;
            esac
            active=0
            enabled=0
            save
        else
            enabled=0
            save
        fi
        ;;
esac
""",
        )

    def process_environment(
        self,
        *,
        failure: str = "",
        implementation: str = "CPython",
        major: int = 3,
        minor: int = 11,
        swap_trigger: str = "",
        swap_path: Path | None = None,
        swap_target: Path | None = None,
        publication_swap: str = "",
    ) -> dict[str, str]:
        self.failure_marker.unlink(missing_ok=True)
        swap_marker = self.tmp_path / "swap.marker"
        swap_marker.unlink(missing_ok=True)
        directory_fail_target = ""
        if failure.startswith("directory-create-"):
            directory_fail_target = str(
                self.managed_directories[int(failure.removeprefix("directory-create-"))]
            )
        mode_fail_target = ""
        if failure == "config-mode":
            mode_fail_target = str(self.config.parent)
        elif failure == "state-mode":
            mode_fail_target = str(self.state_dir)
        noise_path = self.tmp_path / "randomized-transaction-path"
        return {
            **os.environ,
            "PATH": f"{self.fake_bin}:/usr/bin:/bin",
            "FAKE_COMMAND_LOG": str(self.log),
            "FAKE_SERVICE_STATE": str(self.service_state),
            "FAKE_FAILURE_MARKER": str(self.failure_marker),
            "FAKE_ENV_INPUT": str(self.environment),
            "FAKE_SERVICE_INPUT": str(self.linux / "monitor-agent.service"),
            "FAKE_RUNTIME_DIR": str(self.runtime),
            "FAKE_CONFIG_FILE": str(self.config),
            "FAKE_UNIT_FILE": str(self.unit),
            "FAKE_IMPLEMENTATION": implementation,
            "FAKE_MAJOR": str(major),
            "FAKE_MINOR": str(minor),
            "FAKE_SWAP_TRIGGER": swap_trigger,
            "FAKE_SWAP_PATH": str(self.stage if swap_path is None else swap_path),
            "FAKE_SWAP_TARGET": str(
                self.tmp_path / "outside" if swap_target is None else swap_target
            ),
            "FAKE_SWAP_MARKER": str(swap_marker),
            "FAKE_RESTORE_MARKER": str(self.tmp_path / "restore.marker"),
            "FAKE_RESTORE_FAILURE": "",
            "FAKE_NATIVE_SENTINEL": "native-failure-token",
            "FAKE_NATIVE_NOISE": "0",
            "FAKE_ROLLBACK_NOISE_PATH": str(noise_path),
            "FAKE_DIRECTORY_REMOVAL_TARGET": str(self.state_dir),
            "FAKE_STATE_DIR": str(self.state_dir),
            "FAKE_PUBLICATION_SWAP": publication_swap,
            "FAKE_DIRECTORY_FAIL_TARGET": directory_fail_target,
            "FAKE_MODE_FAIL_TARGET": mode_fail_target,
            "FAKE_FAIL_STAGE": (
                "directory-create" if directory_fail_target else
                "directory-mode" if mode_fail_target else failure
            ),
            "MONITOR_AGENT_TEST_BYPASS_ROOT": "1",
        }

    def install(
        self,
        *,
        failure: str = "",
        swap_trigger: str = "",
        swap_path: Path | None = None,
        swap_target: Path | None = None,
        publication_swap: str = "",
        restore_failure: str = "",
    ) -> subprocess.CompletedProcess[str]:
        environment = self.process_environment(
            failure=failure,
            swap_trigger=swap_trigger,
            swap_path=swap_path,
            swap_target=swap_target,
            publication_swap=publication_swap,
        )
        environment["FAKE_RESTORE_FAILURE"] = restore_failure
        if restore_failure:
            environment["FAKE_NATIVE_NOISE"] = "1"
        return _run(
            self.linux / "install.sh",
            self.wheel,
            self.environment,
            env=environment,
        )

    def uninstall(
        self,
        *,
        purge: bool = False,
        failure: str = "",
    ) -> subprocess.CompletedProcess[str]:
        arguments = ("--purge",) if purge else ()
        return _run(
            self.linux / "uninstall.sh",
            *arguments,
            env=self.process_environment(failure=failure),
        )

    def assert_no_temporary_files(self) -> None:
        assert not list(self.stage.rglob(".monitor-agent-install.*"))
        assert not list(self.stage.rglob(".monitor-agent.env.*"))
        assert not list(self.stage.rglob(".monitor-agent.service.*"))


def test_service_sections_have_exact_order_cardinality_and_directives() -> None:
    assert _parse_unit() == EXPECTED_UNIT


def test_service_contains_no_inline_secret_or_live_configuration() -> None:
    text = SERVICE.read_text(encoding="utf-8")
    assert "User=" not in text
    assert "Environment=" not in text
    assert "API_TOKEN" not in text
    assert "COLLECTOR_URI" not in text
    assert "LOG_PATH" not in text


def test_scripts_are_bash_strict_and_executable() -> None:
    for script in (INSTALLER, UNINSTALLER):
        assert script.read_text(encoding="utf-8").splitlines()[:2] == [
            "#!/usr/bin/env bash",
            "set -eu",
        ]
        assert script.stat().st_mode & stat.S_IXUSR


def test_real_scripts_require_actual_root_and_have_no_test_root_surface(tmp_path: Path) -> None:
    if os.geteuid() == 0:
        pytest.skip("never execute a production deployment script as root")
    assert os.geteuid() != 0
    wheel = tmp_path / "monitor_agent-2.0.0-py3-none-any.whl"
    environment_file = tmp_path / "supplied.env"
    wheel.write_bytes(b"wheel fixture")
    environment_file.write_text("MONITOR_API_TOKEN=test\n", encoding="utf-8")
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    _write_executable(fake_bin / "id", "#!/usr/bin/env bash\nprintf '0\\n'\n")
    environment = {**os.environ, "PATH": f"{fake_bin}:/usr/bin:/bin"}
    installer = _run(
        INSTALLER,
        wheel,
        environment_file,
        env=environment,
    )
    uninstaller = _run(UNINSTALLER, env=environment)
    assert installer.returncode == 2
    assert installer.stderr == "monitor-agent install: root privileges required\n"
    assert uninstaller.returncode == 2
    assert uninstaller.stderr == "monitor-agent uninstall: root privileges required\n"
    assert "top-secret-test-token" not in installer.stdout + installer.stderr
    assert "top-secret-test-token" not in uninstaller.stdout + uninstaller.stderr
    for text in (
        INSTALLER.read_text(encoding="utf-8"),
        UNINSTALLER.read_text(encoding="utf-8"),
    ):
        assert "MONITOR_AGENT_TEST_" not in text
        assert "/proc/" not in text
        assert "root_prefix" not in text
        assert "root_anchor" not in text
    installer_text = INSTALLER.read_text(encoding="utf-8")
    assert 'if [ "$EUID" -ne 0 ]; then' in installer_text
    assert 'if [ "$EUID" -ne 0 ]; then' in UNINSTALLER.read_text(encoding="utf-8")
    assert "$(id -u)" not in installer_text + UNINSTALLER.read_text(encoding="utf-8")


def test_installer_argument_guard_is_fixed_and_secret_free(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    wrong_arguments = _run(
        harness.linux / "install.sh",
        harness.wheel,
        env=harness.process_environment(),
    )
    assert wrong_arguments.returncode == 2
    assert wrong_arguments.stderr == "monitor-agent install: expected WHEEL_PATH ENV_FILE\n"
    assert "top-secret-test-token" not in wrong_arguments.stdout + wrong_arguments.stderr


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
    harness = Harness(tmp_path)
    result = _run(
        harness.linux / "install.sh",
        harness.wheel,
        harness.environment,
        env=harness.process_environment(
            implementation=implementation,
            major=major,
            minor=minor,
        ),
    )
    assert (result.returncode == 0) is accepted
    if not accepted:
        assert result.stderr == "monitor-agent install: unsupported Python interpreter\n"
        assert _snapshot(harness.stage) == {}


def _create_invalid_target(path: Path, kind: str, tmp_path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if kind == "directory":
        path.mkdir()
    elif kind == "file":
        path.write_text("invalid target\n", encoding="utf-8")
    elif kind == "fifo":
        os.mkfifo(path)
    elif kind == "socket":
        short_directory = Path(tempfile.mkdtemp(prefix="monitor-agent-t14-"))
        short_socket = short_directory / "s"
        unix_socket = socket.socket(socket.AF_UNIX)
        try:
            try:
                unix_socket.bind(str(short_socket))
            except PermissionError:
                socket_candidates = [
                    candidate
                    for candidate in Path("/tmp/.X11-unix").glob("*")
                    if stat.S_ISSOCK(candidate.stat().st_mode)
                ]
                if not socket_candidates:
                    pytest.skip("sandbox denies AF_UNIX bind and exposes no socket inode")
                os.link(socket_candidates[0], path)
                short_directory.rmdir()
                return
        finally:
            unix_socket.close()
        os.link(short_socket, path)
        short_socket.unlink()
        short_directory.rmdir()
    elif kind == "symlink":
        outside = tmp_path / f"{path.name}-outside"
        outside.write_text("outside\n", encoding="utf-8")
        path.symlink_to(outside)
    else:
        raise AssertionError(kind)


@pytest.mark.parametrize(
    ("target_name", "kind"),
    [
        ("runtime", "file"),
        ("runtime", "fifo"),
        ("runtime", "socket"),
        ("runtime", "symlink"),
        ("environment", "directory"),
        ("environment", "fifo"),
        ("environment", "socket"),
        ("environment", "symlink"),
        ("unit", "directory"),
        ("unit", "fifo"),
        ("unit", "socket"),
        ("unit", "symlink"),
    ],
)
def test_installer_rejects_every_nonregular_live_target_before_mutation(
    tmp_path: Path,
    target_name: str,
    kind: str,
) -> None:
    harness = Harness(tmp_path)
    targets = {
        "runtime": harness.runtime,
        "environment": harness.config,
        "unit": harness.unit,
    }
    _create_invalid_target(targets[target_name], kind, tmp_path)
    before = _snapshot(harness.stage)
    result = harness.install()
    assert result.returncode != 0
    assert result.stderr == "monitor-agent install: invalid install target\n"
    assert _snapshot(harness.stage) == before
    harness.assert_no_temporary_files()


@pytest.mark.parametrize(
    "publication_stage",
    ["runtime-swap", "environment-rename", "unit-rename"],
)
def test_publication_type_swap_restores_exact_snapshot(
    tmp_path: Path,
    publication_stage: str,
) -> None:
    harness = Harness(tmp_path, existing=True, active=True, enabled=True)
    before = _snapshot(harness.stage)
    result = harness.install(publication_swap=publication_stage)
    assert result.returncode != 0
    assert (tmp_path / "swap.marker").exists()
    assert _snapshot(harness.stage) == before
    harness.assert_no_temporary_files()


@pytest.mark.parametrize(
    ("existing", "failure"),
    [
        (False, "cleanup-transaction"),
        (True, "cleanup-environment-backup"),
        (True, "cleanup-unit-backup"),
        (True, "cleanup-transaction"),
    ],
)
def test_success_cleanup_failure_is_nonzero_secret_free_and_retried(
    tmp_path: Path,
    existing: bool,
    failure: str,
) -> None:
    harness = Harness(
        tmp_path,
        existing=existing,
        active=existing,
        enabled=existing,
    )
    result = harness.install(failure=failure)
    assert result.returncode != 0
    assert harness.failure_marker.exists()
    assert result.stdout == ""
    assert result.stderr == "monitor-agent install: cleanup failed\n"
    assert "top-secret-test-token" not in result.stdout + result.stderr
    harness.assert_no_temporary_files()


@pytest.mark.parametrize("existing", [False, True], ids=["fresh", "upgrade"])
@pytest.mark.parametrize("failure", FAILURE_STAGES)
def test_every_install_failure_restores_filesystem_service_and_modes(
    tmp_path: Path,
    existing: bool,
    failure: str,
) -> None:
    harness = Harness(
        tmp_path,
        existing=existing,
        active=existing,
        enabled=existing,
    )
    before_tree = _snapshot(harness.stage)
    before_state = _state(harness.service_state)
    result = harness.install(failure=failure)
    assert result.returncode != 0, failure
    assert harness.failure_marker.exists(), failure
    assert _snapshot(harness.stage) == before_tree, failure
    assert _state(harness.service_state) == before_state, failure
    harness.assert_no_temporary_files()
    assert "monitor-agent install: rollback failed" not in result.stderr
    assert "top-secret-test-token" not in result.stdout + result.stderr


@pytest.mark.parametrize("failure", MANAGED_DIRECTORY_FAILURE_STAGES)
def test_pretransaction_directory_failures_restore_exact_snapshot(
    tmp_path: Path,
    failure: str,
) -> None:
    harness = Harness(tmp_path)
    before_tree = _snapshot(harness.stage)
    before_state = _state(harness.service_state)
    result = harness.install(failure=failure)
    assert result.returncode != 0, failure
    assert harness.failure_marker.exists(), failure
    assert result.stdout == ""
    assert result.stderr == "monitor-agent install: installation failed\n"
    assert _snapshot(harness.stage) == before_tree
    assert _state(harness.service_state) == before_state
    harness.assert_no_temporary_files()
    assert "top-secret-test-token" not in result.stdout + result.stderr


def test_state_mode_failure_follows_config_mode_mutation_and_restores_both(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path, existing=True, active=True, enabled=True)
    before_tree = _snapshot(harness.stage)
    before_state = _state(harness.service_state)
    result = harness.install(failure="state-mode")
    assert result.returncode != 0
    assert harness.failure_marker.exists()
    commands = harness.log.read_text(encoding="utf-8")
    assert f"<0700> <--> <{harness.config.parent}>" in commands
    assert f"<0700> <--> <{harness.state_dir}>" in commands
    assert commands.index(f"<0700> <--> <{harness.config.parent}>") < commands.index(
        f"<0700> <--> <{harness.state_dir}>"
    )
    assert _snapshot(harness.stage) == before_tree
    assert _state(harness.service_state) == before_state


@pytest.mark.parametrize(
    ("restore_failure", "recovery_name", "previous_content"),
    [
        ("runtime", "previous-runtime", "keep runtime\n"),
        ("environment", ".monitor-agent.env.backup.", "MONITOR_API_TOKEN=old-secret\n"),
        ("unit", ".monitor-agent.service.backup.", "old unit\n"),
    ],
)
def test_failed_rollback_retains_recoverable_artifacts(
    tmp_path: Path,
    restore_failure: str,
    recovery_name: str,
    previous_content: str,
) -> None:
    harness = Harness(tmp_path, existing=True, active=True, enabled=True)
    result = harness.install(failure="restart", restore_failure=restore_failure)
    assert result.returncode != 0
    assert harness.failure_marker.exists()
    assert (tmp_path / "restore.marker").exists()
    assert result.stdout == ""
    assert result.stderr == (
        "monitor-agent install: installation failed\n"
        "monitor-agent install: rollback failed\n"
    )
    transactions = list((harness.stage / "opt").glob(".monitor-agent-install.*"))
    assert len(transactions) == 1
    if restore_failure == "runtime":
        recovery = transactions[0] / recovery_name / "old-runtime"
        assert recovery.read_text(encoding="utf-8") == previous_content
    else:
        recoveries = list(harness.stage.rglob(f"{recovery_name}*"))
        assert len(recoveries) == 1
        assert recoveries[0].read_text(encoding="utf-8") == previous_content
    assert "service installed and active" not in result.stdout + result.stderr
    assert "top-secret-test-token" not in result.stdout + result.stderr


@pytest.mark.parametrize(
    "restore_failure",
    ["runtime", "environment", "unit", "service"],
)
def test_failed_restoration_keeps_prior_modes_and_recovery_material(
    tmp_path: Path,
    restore_failure: str,
) -> None:
    active = restore_failure != "service"
    harness = Harness(tmp_path, existing=True, active=active, enabled=True)
    prior_config_mode = stat.S_IMODE(harness.config.parent.stat().st_mode)
    prior_state_mode = stat.S_IMODE(harness.state_dir.stat().st_mode)
    result = harness.install(
        failure="restart",
        restore_failure=restore_failure,
    )
    assert result.returncode != 0
    assert (tmp_path / "restore.marker").exists()
    assert stat.S_IMODE(harness.config.parent.stat().st_mode) == prior_config_mode
    assert stat.S_IMODE(harness.state_dir.stat().st_mode) == prior_state_mode
    assert len(list((harness.stage / "opt").glob(".monitor-agent-install.*"))) == 1


@pytest.mark.parametrize(
    ("failure", "restore_failure", "existing"),
    [
        ("restart", "runtime", True),
        ("restart", "artifact-removal", True),
        ("restart", "environment", True),
        ("restart", "unit", True),
        ("restart", "service", True),
        ("restart", "directory-mode", True),
        ("restart", "directory-removal", False),
    ],
)
def test_rollback_failure_output_is_fixed_and_never_leaks_native_diagnostics(
    tmp_path: Path,
    failure: str,
    restore_failure: str,
    existing: bool,
) -> None:
    active = existing and restore_failure != "service"
    harness = Harness(tmp_path, existing=existing, active=active, enabled=existing)
    result = harness.install(failure=failure, restore_failure=restore_failure)
    assert result.returncode != 0
    assert (tmp_path / "restore.marker").exists()
    assert result.stdout == ""
    assert result.stderr == (
        "monitor-agent install: installation failed\n"
        "monitor-agent install: rollback failed\n"
    )
    assert "native-failure-token" not in result.stdout + result.stderr
    assert "randomized-transaction-path" not in result.stdout + result.stderr


@pytest.mark.parametrize(
    ("active", "enabled"),
    [(True, True), (False, True), (True, False), (False, False)],
)
def test_activation_failure_restores_every_prior_service_state(
    tmp_path: Path,
    active: bool,
    enabled: bool,
) -> None:
    harness = Harness(tmp_path, existing=True, active=active, enabled=enabled)
    before = _snapshot(harness.stage)
    result = harness.install(failure="restart")
    assert result.returncode != 0
    assert _snapshot(harness.stage) == before
    assert _state(harness.service_state) == (int(active), int(enabled))


@pytest.mark.parametrize("failure", ["after-enable", "after-restart", "active-verify"])
def test_absent_service_rollback_explicitly_stops_disables_and_verifies(
    tmp_path: Path,
    failure: str,
) -> None:
    harness = Harness(tmp_path)
    before_tree = _snapshot(harness.stage)
    result = harness.install(failure=failure)
    assert result.returncode != 0, failure
    assert _snapshot(harness.stage) == before_tree
    assert _state(harness.service_state) == (0, 0)
    commands = harness.log.read_text(encoding="utf-8")
    assert "systemctl <stop> <--> <monitor-agent.service>" in commands
    assert "systemctl <disable> <--> <monitor-agent.service>" in commands
    assert commands.count("systemctl <is-active> <--> <monitor-agent.service>") >= 2
    assert commands.count("systemctl <is-enabled> <--> <monitor-agent.service>") >= 2
    harness.assert_no_temporary_files()


@pytest.mark.parametrize(
    "failure",
    [
        "service-state",
        "enabled-state",
        "unexpected-active-state",
        "unexpected-enabled-state",
    ],
)
def test_unexpected_service_state_failure_happens_before_mutation(
    tmp_path: Path,
    failure: str,
) -> None:
    harness = Harness(tmp_path, existing=True, active=True, enabled=True)
    before = _snapshot(harness.stage)
    result = harness.install(failure=failure)
    assert result.returncode != 0
    assert result.stderr == "monitor-agent install: unable to inspect service state\n"
    assert _snapshot(harness.stage) == before


def test_rollback_failure_is_reported_without_claiming_restoration(tmp_path: Path) -> None:
    harness = Harness(tmp_path, existing=True, active=False, enabled=False)
    result = harness.install(failure="rollback-stop")
    assert result.returncode != 0
    assert "monitor-agent install: rollback failed\n" in result.stderr
    assert "top-secret-test-token" not in result.stdout + result.stderr


def test_two_successful_installs_are_fresh_idempotent_and_preserve_state(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path, readme=True)
    first = harness.install()
    assert first.returncode == 0, first.stderr
    harness.state_dir.joinpath("telemetry.json").write_text("keep state\n", encoding="utf-8")
    first_python = harness.runtime / "venv/bin/python"
    first_python.write_text(first_python.read_text() + "\n# replaced\n", encoding="utf-8")

    second = harness.install()

    assert second.returncode == 0, second.stderr
    assert "# replaced" not in first_python.read_text(encoding="utf-8")
    assert harness.state_dir.joinpath("telemetry.json").read_text() == "keep state\n"
    assert harness.config.read_text() == harness.environment.read_text()
    assert harness.runtime.joinpath("README.md").read_text() == "deployment guide\n"
    assert stat.S_IMODE(harness.config.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(harness.config.stat().st_mode) == 0o600
    assert stat.S_IMODE(harness.state_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(harness.unit.stat().st_mode) == 0o644
    assert _state(harness.service_state) == (1, 1)
    commands = harness.log.read_text(encoding="utf-8")
    assert commands.count(" <-m> <venv>") == 2
    assert " <--require-hashes> <-r> " in commands
    assert " <--no-deps> <--force-reinstall> <--> " in commands
    assert "upgrade" not in commands
    harness.assert_no_temporary_files()


def test_environment_is_copied_as_data_and_readme_is_optional(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    side_effect = tmp_path / "environment-executed"
    harness.environment.write_text(
        f"MONITOR_API_TOKEN=$(touch {side_effect})\n",
        encoding="utf-8",
    )
    result = harness.install()
    assert result.returncode == 0, result.stderr
    assert not side_effect.exists()
    assert harness.config.read_text() == harness.environment.read_text()
    assert not harness.runtime.joinpath("README.md").exists()


def test_installer_validates_input_before_any_mutation(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    harness.wheel.unlink()
    result = harness.install()
    assert result.returncode != 0
    assert result.stderr == "monitor-agent install: invalid wheel\n"
    assert _snapshot(harness.stage) == {}


def test_uninstaller_recursive_deletion_surface_is_exact() -> None:
    text = UNINSTALLER.read_text(encoding="utf-8")
    _assert_recursive_deletion_policy(text)
    assert "rm -f -- /etc/systemd/system/monitor-agent.service" in text.splitlines()


def test_root_guard_is_present_before_any_production_path() -> None:
    for script in (INSTALLER, UNINSTALLER):
        text = script.read_text(encoding="utf-8")
        assert text.index('if [ "$EUID" -ne 0 ]; then') < text.index("/opt/monitor-agent")


def test_copied_uninstaller_preserves_config_and_state_by_default_in_order(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path, existing=True, active=True, enabled=True)
    result = harness.uninstall()
    assert result.returncode == 0
    assert result.stdout == "monitor-agent uninstall: service removed\n"
    assert result.stderr == ""
    assert not harness.runtime.exists()
    assert not harness.unit.exists()
    assert harness.config.read_text(encoding="utf-8") == "MONITOR_API_TOKEN=old-secret\n"
    assert (
        harness.state_dir.joinpath("telemetry.json").read_text(encoding="utf-8")
        == "keep state\n"
    )
    assert _state(harness.service_state) == (0, 0)
    assert harness.log.read_text(encoding="utf-8").splitlines() == [
        "systemctl <disable> <--now> <--> <monitor-agent.service>",
        "systemctl <is-active> <--> <monitor-agent.service>",
        "systemctl <is-enabled> <--> <monitor-agent.service>",
        f"rm <-rf> <--> <{harness.runtime}>",
        f"rm <-f> <--> <{harness.unit}>",
        "systemctl <daemon-reload>",
    ]


def test_copied_uninstaller_purges_only_with_explicit_argument(tmp_path: Path) -> None:
    harness = Harness(tmp_path, existing=True, active=True, enabled=True)
    result = harness.uninstall(purge=True)
    assert result.returncode == 0
    assert not harness.runtime.exists()
    assert not harness.unit.exists()
    assert not harness.config.parent.exists()
    assert not harness.state_dir.exists()


def test_copied_uninstaller_accepts_absent_service(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    result = harness.uninstall()
    assert result.returncode == 0
    assert result.stdout == "monitor-agent uninstall: service removed\n"
    assert _state(harness.service_state) == (0, 0)


@pytest.mark.parametrize(
    ("failure", "message"),
    [
        ("uninstall-stop-failure", "monitor-agent uninstall: service is still active\n"),
        ("uninstall-disable-failure", "monitor-agent uninstall: service is still enabled\n"),
        ("uninstall-dbus-failure", "monitor-agent uninstall: unable to verify service inactive\n"),
        ("uninstall-failed-state", "monitor-agent uninstall: unable to verify service inactive\n"),
        ("uninstall-masked-state", "monitor-agent uninstall: unable to verify service disabled\n"),
    ],
)
def test_copied_uninstaller_never_deletes_before_inactive_disabled_proof(
    tmp_path: Path,
    failure: str,
    message: str,
) -> None:
    harness = Harness(tmp_path, existing=True, active=True, enabled=True)
    before = _snapshot(harness.stage)
    result = harness.uninstall(failure=failure)
    assert result.returncode == 2
    assert result.stdout == ""
    assert result.stderr == message
    assert _snapshot(harness.stage) == before
    assert f"{harness.runtime}>" not in harness.log.read_text(encoding="utf-8")
    assert "top-secret-test-token" not in result.stdout + result.stderr


def test_copied_uninstaller_reports_daemon_reload_failure_without_success(tmp_path: Path) -> None:
    harness = Harness(tmp_path, existing=True, active=True, enabled=True)
    result = harness.uninstall(failure="daemon-reload")
    assert result.returncode != 0
    assert result.stdout == ""
    assert result.stderr == "monitor-agent uninstall: unable to reload service manager\n"
    assert not harness.runtime.exists()
    assert not harness.unit.exists()


@pytest.mark.parametrize("arguments", [("unexpected",), ("--purge", "extra")])
def test_copied_uninstaller_rejects_invalid_arguments(
    tmp_path: Path, arguments: tuple[str, ...]
) -> None:
    harness = Harness(tmp_path)
    result = _run(
        harness.linux / "uninstall.sh", *arguments, env=harness.process_environment()
    )
    assert result.returncode == 2
    assert result.stderr == "monitor-agent uninstall: expected no arguments or --purge\n"


@pytest.mark.parametrize(
    "mutant",
    [
        "rm -fr -- /",
        "rm -r -- /tmp/third",
        "rm -R -- /tmp/third",
        "rm --recursive -- /tmp/third",
        "rm --recursive --force -- /",
        "rm --force --recursive -- /tmp/third",
        "rm -fR -- /tmp/third",
        "/bin/rm -rf -- /",
        "command rm -rf -- /",
        "env rm -rf -- /",
        "delete_command='rm -rf -- /'",
        "delete_command=rm\n$delete_command -rf -- /",
        "rm -rf -- \\\n/",
        "rm -rf -- /opt/*",
        "rm -rf -- /opt/monitor-agent /tmp/third",
        "result=$(rm -r -- /tmp/third)",
        "result=`rm -R -- /tmp/third`",
        "wrapper $(command rm -rf -- /tmp/third)",
        'result=$(r""m -r -- /tmp/third)',
        "result=$(outer=$(rm -r -- /tmp/third))",
        'wrapper "$(rm -r -- /tmp/third)"',
        "result=`r\"\"m -R -- /tmp/third`",
        'result=$(tool=r""m; "$tool" -r -- /tmp/third)',
    ],
)
def test_recursive_deletion_policy_helper_rejects_mutants(mutant: str) -> None:
    allowed = "\n".join(shlex.join(command) for command in ALLOWED_RECURSIVE_DELETIONS)
    with pytest.raises(ValueError):
        _assert_recursive_deletion_policy(f"{allowed}\n{mutant}\n")


def test_environment_example_is_exact_and_never_live_configuration() -> None:
    assert ENV_EXAMPLE.read_text(encoding="utf-8").splitlines() == EXPECTED_ENVIRONMENT
    assert ".env.example" not in SERVICE.read_text(encoding="utf-8")
    assert "monitor-agent.env.example" not in INSTALLER.read_text(encoding="utf-8")
