from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WINDOWS = ROOT / "deploy" / "windows"
ISS = WINDOWS / "installer.iss"
GUI_INSTALL = WINDOWS / "gui-install.ps1"
GUI_UNINSTALL = WINDOWS / "gui-uninstall.ps1"

# Windows Defender manipulation / detection-evasion APIs that must never appear.
_FORBIDDEN = (
    "Add-MpPreference",
    "Set-MpPreference",
    "Remove-MpPreference",
    "DisableRealtimeMonitoring",
    "ExclusionPath",
    "ExclusionProcess",
    "Set-MpComputerStatus",
    "MpCmdRun",
    "DisableAntiSpyware",
)


def test_iss_defines_a_gui_wizard_installing_to_program_files() -> None:
    iss = ISS.read_text(encoding="utf-8")
    assert "PrivilegesRequired=admin" in iss
    assert "DefaultDirName={autopf}\\MonitorAgent" in iss
    assert "WizardStyle=modern" in iss
    # A page for the server address and a password field for the token.
    assert "CreateInputQueryPage" in iss
    assert "Agent API token:" in iss
    assert "ConnectionPage.Add('Agent API token:', True)" in iss
    # A notice page defaulting to enabled, mirroring the terminal wizard.
    assert "CreateInputOptionPage" in iss
    assert "NoticePage.Values[0] := True;" in iss
    # HTTPS is enforced before proceeding.
    assert "The server address must use HTTPS." in iss
    # The token is staged to a temp file, not passed on the command line.
    assert "-TokenFile" in iss
    assert "SaveStringToFile(TokenFile" in iss
    assert "-EmployeesNotified" in iss
    assert "gui-install.ps1" in iss
    assert "gui-uninstall.ps1" in iss


def test_gui_install_wires_notice_and_ships_no_secret() -> None:
    script = GUI_INSTALL.read_text(encoding="utf-8")
    assert "mat_" not in script
    assert "[Parameter(Mandatory = $true)][string]$TokenFile" in script
    assert "[switch]$EmployeesNotified" in script
    # Sensitive flags follow the notice switch together.
    assert "MONITOR_SCREENSHOT_ENABLED=$($notified.ToString().ToLower())" in script
    assert "MONITOR_INCLUDE_ACTIVE_WINDOW=$($notified.ToString().ToLower())" in script
    assert "MONITOR_EMPLOYEE_NOTICE_ACK=$($notified.ToString().ToLower())" in script
    # Installs the scheduled task and enforces HTTPS.
    assert 'schtasks.exe /Create /TN "MonitorAgent"' in script
    assert "The server address must use HTTPS." in script


def test_installer_does_not_touch_windows_defender() -> None:
    for path in (ISS, GUI_INSTALL, GUI_UNINSTALL):
        text = path.read_text(encoding="utf-8")
        for token in _FORBIDDEN:
            assert token not in text, f"{path.name} must not reference {token}"


def test_gui_uninstall_removes_tasks() -> None:
    script = GUI_UNINSTALL.read_text(encoding="utf-8")
    assert "schtasks.exe /Delete /TN $task /F" in script
    assert "MonitorAgent" in script
    assert "MonitorAgentSession" in script


def test_release_workflow_builds_the_gui_installer() -> None:
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    assert "choco install innosetup" in workflow
    assert "deploy/windows/installer.iss" in workflow
    assert "MonitorAgent-Windows-$version-Setup.exe" in workflow
