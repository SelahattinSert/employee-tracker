from __future__ import annotations

import importlib.util
import sys
import zipfile
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]
WINDOWS = ROOT / "deploy" / "windows"
CERTIFICATE = b"-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n"


def _builder() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "build_windows_package", ROOT / "scripts" / "build_windows_package.py"
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


def test_package_layout_has_launchers_scripts_lock_and_wheel(tmp_path: Path) -> None:
    builder = _builder()
    target = builder.build_package(tmp_path / "out", wheel=_fake_wheel(tmp_path))
    names = set(zipfile.ZipFile(target).namelist())
    prefix = "MonitorAgent-Windows-2.0.0/"
    assert target.name == "MonitorAgent-Windows-2.0.0.zip"
    assert {prefix + "Install.cmd", prefix + "Uninstall.cmd"} <= names
    assert prefix + "requirements.lock" in names
    assert prefix + "dist/monitor_agent-2.0.0-py3-none-any.whl" in names
    for name in builder.DEPLOY_FILES:
        assert prefix + "deploy/windows/" + name in names
    assert prefix + "ca.crt" not in names
    assert not any(name.endswith("monitor-agent.env") for name in names)


def test_package_bundles_only_public_ca_certificate(tmp_path: Path) -> None:
    builder = _builder()
    ca = tmp_path / "ca.crt"
    ca.write_bytes(CERTIFICATE)
    target = builder.build_package(tmp_path / "out", wheel=_fake_wheel(tmp_path), ca=ca)
    archive = zipfile.ZipFile(target)
    assert archive.read("MonitorAgent-Windows-2.0.0/ca.crt") == CERTIFICATE

    ca.write_bytes(CERTIFICATE + b"-----BEGIN PRIVATE KEY-----\nx\n-----END PRIVATE KEY-----\n")
    with pytest.raises(builder.PackageError, match="private key"):
        builder.build_package(tmp_path / "out", wheel=_fake_wheel(tmp_path), ca=ca)
    ca.write_bytes(b"not a certificate")
    with pytest.raises(builder.PackageError, match="PEM"):
        builder.build_package(tmp_path / "out", wheel=_fake_wheel(tmp_path), ca=ca)


def test_package_rejects_foreign_wheel_and_reports_errors(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    builder = _builder()
    other = tmp_path / "other-1.0-py3-none-any.whl"
    other.write_bytes(b"x")
    with pytest.raises(builder.PackageError, match="monitor_agent"):
        builder.build_package(tmp_path / "out", wheel=other)
    assert builder.main(["--output", str(tmp_path / "out"), "--wheel", str(other)]) == 2
    assert "error:" in capsys.readouterr().err
    assert (
        builder.main(["--output", str(tmp_path / "ok"), "--wheel", str(_fake_wheel(tmp_path))]) == 0
    )


def test_package_builds_wheel_when_not_given(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    builder = _builder()

    def fake_run(command: list[str], check: bool) -> None:
        output = Path(command[command.index("--outdir") + 1])
        (output / "monitor_agent-2.0.0-py3-none-any.whl").write_bytes(b"built")

    monkeypatch.setattr(builder.subprocess, "run", fake_run)
    target = builder.build_package(tmp_path / "out")
    assert "MonitorAgent-Windows-2.0.0/dist/monitor_agent-2.0.0-py3-none-any.whl" in (
        zipfile.ZipFile(target).namelist()
    )


def test_setup_script_takes_secrets_at_install_time_only() -> None:
    setup = (WINDOWS / "setup.ps1").read_text(encoding="utf-8")
    assert 'Read-Host "Agent API token (input is hidden)" -AsSecureString' in setup
    assert "[string]$TokenFile" in setup
    assert "mat_" not in setup
    assert "New-OwnerOnlyDirectory" in setup
    assert "Remove-Item -LiteralPath $WorkDirectory -Recurse -Force" in setup
    assert 'if ($Uri.Scheme -ne "https")' in setup
    assert "--ssl-no-revoke" in setup
    assert "& $Installer -WheelPath $Wheel.FullName -EnvironmentFile $ConfigurationPath" in setup


def test_setup_enables_sensitive_collectors_after_notice_confirmation() -> None:
    setup = (WINDOWS / "setup.ps1").read_text(encoding="utf-8")
    # The notice question defaults to yes (only a leading "n" declines).
    assert "function Read-EmployeesNotified" in setup
    assert 'return ($Answer.Trim() -notmatch "^[nN]")' in setup
    assert "[switch]$EmployeesNotified" in setup
    assert "[switch]$NoEmployeesNotified" in setup
    # A confirmed notice turns the collectors and the compliance flag on together.
    notified_block = setup[
        setup.index("if ($Notified) {") : setup.index("Write-AgentConfiguration -Path")
    ]
    for assignment in (
        '$Settings["MONITOR_EMPLOYEE_NOTICE_ACK"] = "true"',
        '$Settings["MONITOR_SCREENSHOT_ENABLED"] = "true"',
        '$Settings["MONITOR_INCLUDE_ACTIVE_WINDOW"] = "true"',
        '$Settings["MONITOR_AUDIT_PATHS"] = $AuditValue',
    ):
        assert assignment in notified_block
    assert '$DefaultAuditPaths = "C:\\Users"' in setup


def test_agent_code_keeps_sensitive_collectors_off_by_default() -> None:
    # The installer, not the agent, enables sensitive collection, so a plain run stays safe.
    import monitor_agent.config as config

    loaded = config.load_config(
        {
            "MONITOR_COLLECTOR_URI": "https://collector.internal/api/v1/telemetry",
            "MONITOR_API_TOKEN": "token",
        }
    )
    assert loaded.screenshot_enabled is False
    assert loaded.include_active_window is False
    assert loaded.audit_paths == ()
    assert loaded.employee_notice_acknowledged is False

    example = (WINDOWS / "monitor-agent.env.example").read_text(encoding="utf-8")
    assert "MONITOR_SCREENSHOT_ENABLED=false" in example
    assert "MONITOR_INCLUDE_ACTIVE_WINDOW=false" in example
    assert "MONITOR_EMPLOYEE_NOTICE_ACK=false" in example


def test_launchers_elevate_and_use_crlf() -> None:
    for name, target in (("Install.cmd", "setup.ps1"), ("Uninstall.cmd", "uninstall.ps1")):
        raw = (WINDOWS / name).read_bytes()
        text = raw.decode("ascii")
        assert "-Verb RunAs" in text
        assert "-ExecutionPolicy Bypass" in text
        assert f"deploy\\windows\\{target}" in text
    assert (ROOT / ".gitattributes").read_text(encoding="utf-8").strip() == "*.cmd text eol=crlf"


def test_ci_publishes_windows_package_with_build_artifacts() -> None:
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert (
        "python scripts/build_windows_package.py "
        "--wheel dist/monitor_agent-2.0.0-py3-none-any.whl --output dist"
    ) in workflow
