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
]

ALLOWED_RECURSIVE_DELETIONS = [
    ("rm", "-rf", "--", "/opt/monitor-agent"),
    ("rm", "-rf", "--", "/etc/monitor-agent", "/var/lib/monitor-agent"),
]


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
        recursive_option = "r" in short_options or "--recursive" in arguments
        force_option = "f" in short_options or "--force" in arguments
        if not (recursive_option and force_option):
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
        shutil.copy2(INSTALLER, self.linux / "install.sh")
        shutil.copy2(UNINSTALLER, self.linux / "uninstall.sh")
        shutil.copy2(SERVICE, self.linux / "monitor-agent.service")
        shutil.copy2(ROOT / "requirements.lock", self.project / "requirements.lock")
        if readme:
            (self.project / "README.md").write_text("deployment guide\n", encoding="utf-8")

        self.stage = tmp_path / "stage"
        self.stage.mkdir(mode=0o700)
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
            self.fake_bin / "id",
            """#!/usr/bin/env bash
set -eu
printf '%s\n' "${FAKE_UID:-0}"
""",
        )
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
            self.fake_bin / "mv",
            """#!/usr/bin/env bash
set -eu
printf 'mv' >> "$FAKE_COMMAND_LOG"
printf ' <%s>' "$@" >> "$FAKE_COMMAND_LOG"
printf '\n' >> "$FAKE_COMMAND_LOG"
destination="${!#}"
source="${@: -2:1}"
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
    exit 90
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
        if [ "$unit_file_present" -eq 0 ]; then
            active=0
            enabled=0
            save
        fi
        exit 0
        ;;
    enable)
        fail_once enable && exit 79
        enabled=1
        save
        ;;
    restart | start)
        fail_once restart && exit 80
        if [ "${FAKE_FAIL_STAGE:-}" = "rollback-start" ] \
            && [ -e "$FAKE_FAILURE_MARKER" ]; then
            exit 81
        fi
        active=1
        save
        ;;
    stop)
        if [ "${FAKE_FAIL_STAGE:-}" = "rollback-stop" ] \
            && [ -e "$FAKE_FAILURE_MARKER" ]; then
            exit 82
        fi
        if [ "$present" -eq 0 ]; then
            printf 'not-found\n'
            exit 4
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
            if [ "$present" -eq 0 ]; then
                printf 'not-found\n'
                exit 4
            fi
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
        uid: int = 0,
        root: Path | str | None = None,
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
        return {
            **os.environ,
            "PATH": f"{self.fake_bin}:/usr/bin:/bin",
            "FAKE_UID": str(uid),
            "FAKE_COMMAND_LOG": str(self.log),
            "FAKE_SERVICE_STATE": str(self.service_state),
            "FAKE_FAILURE_MARKER": str(self.failure_marker),
            "FAKE_FAIL_STAGE": failure,
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
            "FAKE_PUBLICATION_SWAP": publication_swap,
            "MONITOR_AGENT_TEST_MODE": "1",
            "MONITOR_AGENT_TEST_ROOT": str(self.stage if root is None else root),
        }

    def install(
        self,
        *,
        failure: str = "",
        root: Path | str | None = None,
        swap_trigger: str = "",
        swap_path: Path | None = None,
        swap_target: Path | None = None,
        publication_swap: str = "",
    ) -> subprocess.CompletedProcess[str]:
        return _run(
            self.linux / "install.sh",
            self.wheel,
            self.environment,
            env=self.process_environment(
                failure=failure,
                root=root,
                swap_trigger=swap_trigger,
                swap_path=swap_path,
                swap_target=swap_target,
                publication_swap=publication_swap,
            ),
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


def test_installer_root_and_argument_guards_are_fixed_and_secret_free(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    non_root_environment = harness.process_environment(uid=0)
    non_root_environment.pop("MONITOR_AGENT_TEST_MODE")
    non_root_environment.pop("MONITOR_AGENT_TEST_ROOT")
    non_root = _run(
        harness.linux / "install.sh",
        harness.wheel,
        harness.environment,
        env=non_root_environment,
    )
    assert non_root.returncode == 2
    assert non_root.stderr == "monitor-agent install: root privileges required\n"
    wrong_arguments = _run(
        harness.linux / "install.sh",
        harness.wheel,
        env=harness.process_environment(),
    )
    assert wrong_arguments.returncode == 2
    assert wrong_arguments.stderr == "monitor-agent install: expected WHEEL_PATH ENV_FILE\n"
    assert "top-secret-test-token" not in non_root.stdout + non_root.stderr
    script = INSTALLER.read_text(encoding="utf-8")
    assert "EUID" in script
    assert "$(id -u)" not in script


@pytest.mark.skipif(os.geteuid() != 0, reason="requires real root EUID")
def test_real_root_rejects_nonempty_test_root(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    result = harness.install()
    assert result.returncode == 2
    assert result.stderr == "monitor-agent install: staging root forbidden for root\n"


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


@pytest.mark.parametrize(
    "invalid_root",
    ["/", "relative/root", "/tmp/.."],
)
def test_installer_rejects_uncontained_staging_roots_before_mutation(
    tmp_path: Path,
    invalid_root: str,
) -> None:
    harness = Harness(tmp_path)
    before = _snapshot(harness.stage)
    result = _run(
        harness.linux / "install.sh",
        harness.wheel,
        harness.environment,
        env=harness.process_environment(failure="any-install", root=invalid_root),
    )
    assert result.returncode != 0
    assert result.stderr == "monitor-agent install: invalid staging root\n"
    assert _snapshot(harness.stage) == before
    assert not any(
        line.startswith(("install", "mv")) for line in harness.log.read_text().splitlines()
    )


def test_installer_rejects_symlink_staging_root_before_mutation(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    link = tmp_path / "root-link"
    link.symlink_to("/")
    result = _run(
        harness.linux / "install.sh",
        harness.wheel,
        harness.environment,
        env=harness.process_environment(failure="any-install", root=link),
    )
    assert result.returncode != 0
    assert result.stderr == "monitor-agent install: invalid staging root\n"
    assert not any(
        line.startswith(("install", "mv")) for line in harness.log.read_text().splitlines()
    )


def test_installer_rejects_derived_symlink_escape_before_mutation(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (harness.stage / "opt").symlink_to(outside)
    before_stage = _snapshot(harness.stage)
    before_outside = _snapshot(outside)
    result = harness.install(failure="any-install")
    assert result.returncode != 0
    assert result.stderr == "monitor-agent install: invalid staging root\n"
    assert _snapshot(harness.stage) == before_stage
    assert _snapshot(outside) == before_outside


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
    ("trigger", "ancestor"),
    [
        ("service-state", "root"),
        ("service-state", "opt"),
        ("service-state", "etc"),
        ("service-state", "var"),
        ("runtime-swap", "opt"),
        ("environment-rename", "config"),
        ("unit-rename", "unit"),
        ("success-cleanup", "root"),
    ],
)
def test_owned_test_root_swaps_never_mutate_outside_tree(
    tmp_path: Path,
    trigger: str,
    ancestor: str,
) -> None:
    harness = Harness(tmp_path, existing=True, active=True, enabled=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    swap_paths = {
        "root": harness.stage,
        "opt": harness.stage / "opt",
        "etc": harness.stage / "etc",
        "var": harness.stage / "var",
        "config": harness.config.parent,
        "unit": harness.unit.parent,
    }
    before_outside = _snapshot(outside)
    result = harness.install(
        swap_trigger=trigger,
        swap_path=swap_paths[ancestor],
        swap_target=outside,
    )
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "swap.marker").exists()
    assert _snapshot(outside) == before_outside
    assert "top-secret-test-token" not in result.stdout + result.stderr


def test_rollback_deletion_root_swap_never_mutates_outside_tree(tmp_path: Path) -> None:
    harness = Harness(tmp_path, existing=True, active=True, enabled=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    before_outside = _snapshot(outside)
    result = harness.install(
        failure="restart",
        swap_trigger="rollback-delete",
        swap_path=harness.stage / "opt",
        swap_target=outside,
    )
    assert result.returncode != 0
    assert (tmp_path / "swap.marker").exists()
    assert _snapshot(outside) == before_outside


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


@pytest.mark.parametrize(
    ("existing", "failure", "expected_error"),
    [
        (False, "", ""),
        (True, "", ""),
        (True, "uninstall-stop-failure", "service is still active"),
        (True, "uninstall-disable-failure", "service is still enabled"),
        (True, "uninstall-dbus-failure", "unable to verify service inactive"),
        (True, "uninstall-failed-state", "unable to verify service inactive"),
        (True, "uninstall-masked-state", "unable to verify service disabled"),
    ],
)
def test_uninstall_verifies_inactive_and_disabled_before_deletion(
    tmp_path: Path,
    existing: bool,
    failure: str,
    expected_error: str,
) -> None:
    harness = Harness(tmp_path, existing=existing, active=existing, enabled=existing)
    result = harness.uninstall(failure=failure)
    commands = harness.log.read_text(encoding="utf-8")
    if expected_error:
        assert result.returncode != 0
        assert result.stderr == f"monitor-agent uninstall: {expected_error}\n"
        assert "rm <-rf> <--> </opt/monitor-agent>" not in commands
        assert "rm <-f> <--> </etc/systemd/system/monitor-agent.service>" not in commands
    else:
        assert result.returncode == 0, result.stderr
        assert "systemctl <is-active> <--> <monitor-agent.service>" in commands
        assert "systemctl <is-enabled> <--> <monitor-agent.service>" in commands
        assert "rm <-rf> <--> </opt/monitor-agent>" in commands
        assert "rm <-f> <--> </etc/systemd/system/monitor-agent.service>" in commands


def test_uninstall_guards_arguments_preserves_by_default_and_purges_explicitly(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path, existing=True, active=True, enabled=True)
    non_root = _run(
        harness.linux / "uninstall.sh",
        env=harness.process_environment(uid=1000),
    )
    assert non_root.returncode == 2
    wrong = _run(
        harness.linux / "uninstall.sh",
        "--force",
        env=harness.process_environment(),
    )
    assert wrong.returncode == 2

    default = harness.uninstall()
    assert default.returncode == 0
    default_commands = harness.log.read_text(encoding="utf-8")
    assert "rm <-rf> <--> </etc/monitor-agent> </var/lib/monitor-agent>" not in default_commands

    harness.log.write_text("", encoding="utf-8")
    purge = harness.uninstall(purge=True)
    assert purge.returncode == 0
    assert (
        "rm <-rf> <--> </etc/monitor-agent> </var/lib/monitor-agent>"
        in harness.log.read_text(encoding="utf-8")
    )


def test_uninstaller_recursive_deletion_surface_is_exact() -> None:
    text = UNINSTALLER.read_text(encoding="utf-8")
    _assert_recursive_deletion_policy(text)
    assert "rm -f -- /etc/systemd/system/monitor-agent.service" in text.splitlines()


@pytest.mark.parametrize(
    "mutant",
    [
        "rm -fr -- /",
        "rm --recursive --force -- /",
        "/bin/rm -rf -- /",
        "command rm -rf -- /",
        "env rm -rf -- /",
        "delete_command='rm -rf -- /'",
        "delete_command=rm\n$delete_command -rf -- /",
        "rm -rf -- \\\n/",
        "rm -rf -- /opt/*",
        "rm -rf -- /opt/monitor-agent /tmp/third",
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
