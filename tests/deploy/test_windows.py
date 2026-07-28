from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from xml.etree import ElementTree

import pytest

ROOT = Path(__file__).resolve().parents[2]
WINDOWS = ROOT / "deploy" / "windows"
TASK_XML = WINDOWS / "monitor_agent_task.xml"
LAUNCHER = WINDOWS / "run-agent.ps1"
INSTALLER = WINDOWS / "install.ps1"
UNINSTALLER = WINDOWS / "uninstall.ps1"
ENV_EXAMPLE = WINDOWS / "monitor-agent.env.example"
NAMESPACE = "http://schemas.microsoft.com/windows/2004/02/mit/task"
KNOWN_ENVIRONMENT_KEYS = {
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


def _tag(name: str) -> str:
    return f"{{{NAMESPACE}}}{name}"


def _script(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_task_xml_is_utf8_schema_14_and_has_one_complete_action() -> None:
    raw = TASK_XML.read_bytes()
    assert raw.startswith(b'<?xml version="1.0" encoding="UTF-8"?>')
    assert not raw.startswith(b"\xef\xbb\xbf")
    root = ElementTree.fromstring(raw)
    assert root.tag == _tag("Task")
    assert root.attrib == {"version": "1.4"}
    assert len(root.findall(_tag("Triggers"))) == 1
    assert len(root.findall(_tag("Principals"))) == 1
    assert len(root.findall(_tag("Settings"))) == 1
    actions = root.findall(_tag("Actions"))
    assert len(actions) == 1
    assert actions[0].attrib == {"Context": "System"}
    execs = actions[0].findall(_tag("Exec"))
    assert len(execs) == 1
    assert execs[0].findtext(_tag("Command")) == "powershell.exe"
    assert execs[0].findtext(_tag("Arguments")) == (
        '-NoProfile -NonInteractive -ExecutionPolicy Bypass -File '
        '"C:\\ProgramData\\MonitorAgent\\run-agent.ps1"'
    )
    assert "C:\\Python" not in raw.decode("utf-8")


def test_task_xml_runs_at_boot_as_localsystem_with_restarts() -> None:
    root = ElementTree.fromstring(TASK_XML.read_bytes())
    trigger = root.find(f"{_tag('Triggers')}/{_tag('BootTrigger')}")
    assert trigger is not None
    assert trigger.findtext(_tag("Enabled")) == "true"
    assert trigger.findtext(_tag("Delay")) == "PT30S"
    principal = root.find(f"{_tag('Principals')}/{_tag('Principal')}")
    assert principal is not None
    assert principal.attrib == {"id": "System"}
    assert principal.findtext(_tag("UserId")) == "S-1-5-18"
    assert principal.findtext(_tag("LogonType")) == "ServiceAccount"
    assert principal.findtext(_tag("RunLevel")) == "HighestAvailable"
    settings = root.find(_tag("Settings"))
    assert settings is not None
    assert settings.findtext(_tag("MultipleInstancesPolicy")) == "IgnoreNew"
    assert settings.findtext(_tag("DisallowStartIfOnBatteries")) == "false"
    assert settings.findtext(_tag("StopIfGoingOnBatteries")) == "false"
    assert settings.findtext(_tag("StartWhenAvailable")) == "true"
    assert settings.findtext(_tag("ExecutionTimeLimit")) == "PT0S"
    restart = settings.find(_tag("RestartOnFailure"))
    assert restart is not None
    assert restart.findtext(_tag("Interval")) == "PT30S"
    assert restart.findtext(_tag("Count")) == "5"
    assert settings.findtext(_tag("Enabled")) == "true"
    assert settings.findtext(_tag("Hidden")) == "false"


def test_launcher_strictly_parses_known_environment_and_runs_entry_point() -> None:
    text = _script(LAUNCHER)
    assert '[ValidateSet("run", "check-config", "health")]' in text
    assert '[string]$Command = "run"' in text
    for key in KNOWN_ENVIRONMENT_KEYS:
        assert f'"{key}"' in text
    assert "Invoke-Expression" not in text
    assert not re.search(r"(?m)^\s*\.\s+\$", text)
    assert "$Trimmed.StartsWith(\"#\")" in text
    assert '$Trimmed.Split("=", 2)' in text
    assert "$KnownEnvironmentKeys -notcontains $Parts[0]" in text
    assert "$SeenEnvironmentKeys.Contains($Parts[0])" in text
    assert "IndexOf([char]0)" in text
    assert "Clear-KnownEnvironment" in text
    assert "Test-RegularFile" in text
    assert "ReparsePoint" in text
    assert '& $AgentPath $Command' in text
    assert "exit $LASTEXITCODE" in text


def test_installer_stages_validates_rolls_back_and_locks_acl() -> None:
    text = _script(INSTALLER)
    assert '[ValidateSet("3.11", "3.12", "3.13", "3.14")]' in text
    assert '[string]$PythonVersion = "3.11"' in text
    assert 'py "-$PythonVersion" -m venv $StageVenv' in text
    assert "Test-RegularFile" in text
    assert "Test-ReparsePoint" in text
    assert "Test-PathCommand" in text
    assert "requirements.lock" in text
    assert "--require-hashes" in text
    assert "--no-deps --force-reinstall" in text
    assert "install --upgrade pip" not in text
    assert "MonitorAgent" in text
    assert "C:\\ProgramData\\MonitorAgent" in text
    assert "Transaction" in text
    assert "Rollback" in text
    assert "check-config" in text
    assert "schtasks" in text
    assert "/Create /TN MonitorAgent" in text
    assert "/Run /TN MonitorAgent" in text
    assert "/Query /TN MonitorAgent" in text
    assert "*S-1-5-18:(OI)(CI)F" in text
    assert "*S-1-5-32-544:(OI)(CI)F" in text
    assert "icacls" in text
    assert '"/reset"' in text
    assert "Get-Acl" in text
    assert "DACL verification failed" in text
    assert "Set-RestrictedAcl $TransactionRoot" in text
    assert "Set-RestrictedAcl $BackupRoot" in text
    assert "Get-TaskNotFound" in text
    assert "Invoke-Rollback" in text
    assert "Remove-SafePath $BackupRoot" in text
    assert "logs" in text and "spool" in text
    assert "Administrator" in text


def test_uninstaller_preserves_state_by_default_and_verifies_task_removal() -> None:
    text = _script(UNINSTALLER)
    assert "param([switch]$Purge)" in text
    assert "Administrator" in text
    assert "/Query /TN MonitorAgent" in text
    assert "/End /TN MonitorAgent" in text
    assert "/Delete /TN MonitorAgent /F" in text
    assert "Verify-TaskAbsent" in text
    assert "Remove-RuntimeArtifacts" in text
    assert "monitor-agent.env" in text
    assert "logs" in text and "spool" in text
    assert "Remove-Item -LiteralPath $InstallRoot -Recurse -Force" in text
    assert "SilentlyContinue" not in text
    assert "ReparsePoint" in text


def test_windows_environment_example_has_safe_baseline_values() -> None:
    values = dict(
        line.split("=", 1)
        for line in _script(ENV_EXAMPLE).splitlines()
        if line and not line.startswith("#")
    )
    assert values["MONITOR_COLLECTOR_URI"] == "https://collector.internal/api/v1/telemetry"
    assert values["MONITOR_API_TOKEN"] == "replace-with-managed-secret"
    assert values["MONITOR_HEARTBEAT_SEC"] == "300"
    assert values["MONITOR_STARTUP_DELAY_SEC"] == "30"
    assert values["MONITOR_SPOOL_PATH"] == r"C:\ProgramData\MonitorAgent\spool"
    assert values["MONITOR_PROCESS_CMDLINE_MODE"] == "redacted"
    assert values["MONITOR_LOG_PATH"] == r"C:\ProgramData\MonitorAgent\logs\monitor-agent.log"
    assert values["MONITOR_LOG_FORMAT"] == "json"
    assert values["MONITOR_API_TOKEN"] != ""


def test_powershell_parser_and_environment_runtime_are_skipped_without_pwsh() -> None:
    pwsh = shutil.which("pwsh")
    if pwsh is None:
        pytest.skip("pwsh is unavailable on this Linux host; validate with Windows PowerShell")
    for script in (LAUNCHER, INSTALLER, UNINSTALLER):
        result = subprocess.run(
            [
                pwsh,
                "-NoProfile",
                "-Command",
                "param($path) $errors = $null; "
                "[System.Management.Automation.Language.Parser]::ParseFile("
                "$path, [ref]$null, [ref]$errors) | Out-Null; "
                "if ($errors.Count) { $errors | ForEach-Object { $_.ToString() }; exit 1 }",
                "--",
                str(script),
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr or result.stdout

    with tempfile.TemporaryDirectory() as temporary_directory:
        install_root = Path(temporary_directory) / "MonitorAgent"
        config = install_root / "monitor-agent.env"
        agent = install_root / "venv" / "Scripts" / "monitor-agent.exe"
        agent.parent.mkdir(parents=True)
        # A regular but non-executable placeholder is enough: successful parsing
        # reaches invocation, while malformed files fail before it is considered.
        agent.write_bytes(b"not-an-executable")

        def run_launcher(contents: str) -> subprocess.CompletedProcess[str]:
            config.parent.mkdir(parents=True, exist_ok=True)
            config.write_text(contents, encoding="utf-8")
            return subprocess.run(
                [
                    pwsh,
                    "-NoProfile",
                    "-File",
                    str(LAUNCHER),
                    "-Command",
                    "check-config",
                    "-InstallRoot",
                    str(install_root),
                ],
                check=False,
                capture_output=True,
                text=True,
            )

        valid = run_launcher(
            "MONITOR_COLLECTOR_URI=https://collector.internal/api/v1/telemetry\n"
            "MONITOR_API_TOKEN=token=with=equals\n"
        )
        assert "Invalid Monitor Agent environment entry" not in valid.stderr
        assert "token=with=equals" not in valid.stderr
        for invalid_contents in (
            "UNEXPECTED_SETTING=value\n",
            "MONITOR_API_TOKEN=first\nMONITOR_API_TOKEN=second\n",
            " MONITOR_API_TOKEN=value\n",
        ):
            invalid = run_launcher(invalid_contents)
            assert invalid.returncode != 0
            assert "Invalid Monitor Agent environment entry" in invalid.stderr
