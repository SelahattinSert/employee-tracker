from __future__ import annotations

import importlib.util
import stat
import sys
import tarfile
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "deploy"
CERTIFICATE = b"-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n"


def _builder() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "build_unix_package", ROOT / "scripts" / "build_unix_package.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _fake_wheel(tmp_path: Path) -> Path:
    wheel = tmp_path / "monitor_agent-2.0.0-py3-none-any.whl"
    wheel.write_bytes(b"wheel")
    return wheel


@pytest.mark.parametrize(
    ("os_name", "label", "launcher", "installer", "example"),
    [
        (
            "linux",
            "Linux",
            "Install.sh",
            "deploy/linux/install.sh",
            "deploy/linux/monitor-agent.env.example",
        ),
        (
            "macos",
            "macOS",
            "Install.command",
            "deploy/macos/install.sh",
            "deploy/macos/monitor-agent.env.example",
        ),
    ],
)
def test_package_layout_and_executable_bits(
    tmp_path: Path, os_name: str, label: str, launcher: str, installer: str, example: str
) -> None:
    builder = _builder()
    target = builder.build_package(os_name, tmp_path / "out", wheel=_fake_wheel(tmp_path))
    assert target.name == f"MonitorAgent-{label}-2.0.0.tar.gz"
    prefix = f"MonitorAgent-{label}-2.0.0/"
    with tarfile.open(target) as archive:
        members = {m.name: m for m in archive.getmembers()}
    for name in (
        launcher,
        installer,
        f"deploy/{os_name}/setup.sh",
        "deploy/common/setup-wizard.sh",
        example,
        "requirements.lock",
        "dist/monitor_agent-2.0.0-py3-none-any.whl",
    ):
        assert prefix + name in members
    assert prefix + "ca.crt" not in members
    assert not any(name.endswith("monitor-agent.env") for name in members)
    # Launchers and setup.sh are executable; the sourced wizard and data files are not.
    assert members[prefix + launcher].mode & stat.S_IXUSR
    assert members[prefix + f"deploy/{os_name}/setup.sh"].mode & stat.S_IXUSR
    assert not members[prefix + "deploy/common/setup-wizard.sh"].mode & stat.S_IXUSR
    assert not members[prefix + "requirements.lock"].mode & stat.S_IXUSR


def test_package_bundles_only_public_ca(tmp_path: Path) -> None:
    builder = _builder()
    ca = tmp_path / "ca.crt"
    ca.write_bytes(CERTIFICATE)
    target = builder.build_package("linux", tmp_path / "out", wheel=_fake_wheel(tmp_path), ca=ca)
    with tarfile.open(target) as archive:
        extracted = archive.extractfile("MonitorAgent-Linux-2.0.0/ca.crt")
        assert extracted is not None
        assert extracted.read() == CERTIFICATE

    ca.write_bytes(CERTIFICATE + b"-----BEGIN PRIVATE KEY-----\nx\n-----END PRIVATE KEY-----\n")
    with pytest.raises(builder.PackageError, match="private key"):
        builder.build_package("linux", tmp_path / "out", wheel=_fake_wheel(tmp_path), ca=ca)
    ca.write_bytes(b"nope")
    with pytest.raises(builder.PackageError, match="PEM"):
        builder.build_package("macos", tmp_path / "out", wheel=_fake_wheel(tmp_path), ca=ca)


def test_builder_rejects_bad_inputs(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    builder = _builder()
    with pytest.raises(builder.PackageError, match="unsupported OS"):
        builder.build_package("solaris", tmp_path / "out", wheel=_fake_wheel(tmp_path))
    other = tmp_path / "other-1.0-py3-none-any.whl"
    other.write_bytes(b"x")
    with pytest.raises(builder.PackageError, match="monitor_agent"):
        builder.build_package("linux", tmp_path / "out", wheel=other)
    assert (
        builder.main(["--os", "linux", "--output", str(tmp_path / "o"), "--wheel", str(other)]) == 2
    )
    assert "error:" in capsys.readouterr().err
    assert (
        builder.main(
            [
                "--os",
                "macos",
                "--output",
                str(tmp_path / "ok"),
                "--wheel",
                str(_fake_wheel(tmp_path)),
            ]
        )
        == 0
    )


def test_builder_builds_wheel_when_not_given(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    builder = _builder()

    def fake_run(command: list[str], check: bool) -> None:
        output = Path(command[command.index("--outdir") + 1])
        (output / "monitor_agent-2.0.0-py3-none-any.whl").write_bytes(b"built")

    monkeypatch.setattr(builder.subprocess, "run", fake_run)
    target = builder.build_package("linux", tmp_path / "out")
    with tarfile.open(target) as archive:
        names = archive.getnames()
    assert "MonitorAgent-Linux-2.0.0/dist/monitor_agent-2.0.0-py3-none-any.whl" in names


def test_wizard_asks_notice_and_defaults_yes() -> None:
    wizard = (DEPLOY / "common" / "setup-wizard.sh").read_text(encoding="utf-8")
    # Only a leading n/N declines; everything else (including empty) enables.
    assert "Have employees been notified? Enable these collectors? [Y/n]" in wizard
    assert "[nN]*) return 1" in wizard
    assert "--no-employees-notified) setup_notified_opt=no" in wizard
    assert "--employees-notified) setup_notified_opt=yes" in wizard
    # When notified, the collectors and the compliance flag are written together.
    for fragment in (
        "MONITOR_SCREENSHOT_ENABLED=true",
        "MONITOR_INCLUDE_ACTIVE_WINDOW=true",
        "MONITOR_EMPLOYEE_NOTICE_ACK=true",
        "MONITOR_AUDIT_PATHS=%s",
    ):
        assert fragment in wizard
    # Secrets are read at install time, not shipped.
    assert "Agent API token (input is hidden)" in wizard
    assert "mktemp -d" in wizard
    assert 'rm -rf "$setup_workdir"' in wizard
    assert "the server address must use HTTPS" in wizard


def test_os_setup_scripts_wire_defaults() -> None:
    linux = (DEPLOY / "linux" / "setup.sh").read_text(encoding="utf-8")
    macos = (DEPLOY / "macos" / "setup.sh").read_text(encoding="utf-8")
    assert 'SETUP_DEFAULT_AUDIT_PATHS="/home"' in linux
    assert 'SETUP_DEFAULT_AUDIT_PATHS="/Users"' in macos
    for script in (linux, macos):
        assert 'SETUP_INSTALLER="$SETUP_DIR/install.sh"' in script
        assert '. "$SETUP_DIR/../common/setup-wizard.sh"' in script


def test_agent_defaults_keep_sensitive_collectors_off() -> None:
    import monitor_agent.config as config

    loaded = config.load_config(
        {
            "MONITOR_COLLECTOR_URI": "https://collector.internal/api/v1/telemetry",
            "MONITOR_API_TOKEN": "token",
        }
    )
    assert loaded.screenshot_enabled is False
    assert loaded.include_active_window is False
    assert loaded.employee_notice_acknowledged is False
    for example in (
        DEPLOY / "linux" / "monitor-agent.env.example",
        DEPLOY / "macos" / "monitor-agent.env.example",
    ):
        text = example.read_text(encoding="utf-8")
        assert "MONITOR_SCREENSHOT_ENABLED=false" in text
        assert "MONITOR_EMPLOYEE_NOTICE_ACK=false" in text


def test_ci_builds_and_shellchecks_unix_packages() -> None:
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    for os_name in ("linux", "macos"):
        assert (
            f"python scripts/build_unix_package.py --os {os_name} "
            "--wheel dist/monitor_agent-2.0.0-py3-none-any.whl --output dist"
        ) in workflow
    assert (
        "shellcheck -s sh deploy/common/setup-wizard.sh deploy/linux/setup.sh "
        "deploy/macos/setup.sh deploy/linux/Install.sh deploy/macos/Install.command"
    ) in workflow
