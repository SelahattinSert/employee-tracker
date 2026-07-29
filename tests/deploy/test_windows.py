from __future__ import annotations

import base64
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


def _assert_ordered(text: str, *needles: str) -> None:
    positions = [text.index(needle) for needle in needles]
    assert positions == sorted(positions), dict(zip(needles, positions, strict=True))


def _function(text: str, name: str) -> str:
    match = re.search(rf"(?m)^function {re.escape(name)} \{{", text)
    assert match is not None, f"missing PowerShell function {name}"
    depth = 0
    for index in range(match.start(), len(text)):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                return text[match.start() : index + 1]
    raise AssertionError(f"unclosed PowerShell function {name}")


def _ps_literal(path: Path) -> str:
    return "'" + str(path).replace("'", "''") + "'"


def _replace_function(text: str, name: str, replacement: str) -> str:
    return text.replace(_function(text, name), replacement)


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


def test_installer_stages_validates_and_uses_hash_locked_python_versions() -> None:
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
    assert "check-config" in text
    assert "*S-1-5-18:(OI)(CI)F" in text
    assert "*S-1-5-32-544:(OI)(CI)F" in text
    assert "icacls" in text
    assert '"/reset"' in text
    assert "Get-Acl" in text
    assert "DACL verification failed" in text
    assert "Set-RestrictedAcl $TransactionRoot" in text
    assert "Set-RestrictedAcl $BackupRoot" in text
    assert "Invoke-Rollback" in text
    assert "logs" in text and "spool" in text
    assert "Administrator" in text


def test_installer_preflights_every_required_windows_command() -> None:
    text = _script(INSTALLER)
    live_flow = text[text.index("try {\n    $Principal") :]
    command_check = live_flow[: live_flow.index("Assert-SafeDirectory $InstallParent")]
    assert 'Test-PathCommand "py"' in command_check
    assert 'Test-PathCommand "schtasks.exe"' in command_check
    assert 'Test-PathCommand "icacls.exe"' in command_check


def test_restricted_acl_verifies_inheritance_is_protected() -> None:
    acl_function = _function(_script(INSTALLER), "Set-RestrictedAcl")
    assert ".AreAccessRulesProtected" in acl_function
    assert 'Fail "DACL verification failed"' in acl_function


def test_installer_uses_locale_independent_task_scheduler_state() -> None:
    text = _script(INSTALLER)
    assert 'New-Object -ComObject "Schedule.Service"' in text
    assert "$TaskNotFoundHResult = -2147024894" in text
    assert "$TaskStateQueued = 2" in text
    assert "$TaskStateReady = 3" in text
    assert "$TaskStateRunning = 4" in text
    assert ".Exception.HResult -eq $TaskNotFoundHResult" in text
    assert "[int]$RegisteredTask.State" in text
    assert text.count('Test-PathCommand "schtasks.exe"') == 1
    assert not re.search(r"(?m)^\s*&\s*schtasks", text, re.IGNORECASE)
    assert "-match \"Running\"" not in text
    assert "cannot find" not in text.lower()
    assert "does not exist" not in text.lower()


def test_installer_preserves_custom_registered_task_sddl_on_rollback() -> None:
    text = _script(INSTALLER)
    rollback = _function(text, "Invoke-Rollback")
    rollback_verification = _function(text, "Test-RollbackState")
    assert "$TaskSecurityOwner = 0x1" in text
    assert "$TaskSecurityGroup = 0x2" in text
    assert "$TaskSecurityDacl = 0x4" in text
    assert "$TaskSecurityInformation = (" in text
    assert "$TaskDontAddPrincipalAce = 0x10" in text
    assert "$TaskRestoreFlags = (" in text
    assert "$PriorTask.GetSecurityDescriptor($TaskSecurityInformation)" in text
    assert "$PriorTaskSddl" in rollback
    register_call = rollback[rollback.index("$TaskFolder.RegisterTask(") :]
    assert "$TaskRestoreFlags" in register_call
    assert re.search(
        r"\$TaskLogonServiceAccount,\s*\$PriorTaskSddl\s*\)",
        register_call,
    )
    assert "$ObservedTask.GetSecurityDescriptor($TaskSecurityInformation)" in (
        rollback_verification
    )
    capture_block = text[
        text.index("if ($PriorTaskWasPresent) {") : text.index(
            "$MutationStarted = $true"
        )
    ]
    assert "GetSecurityDescriptor" in capture_block


def test_installer_exports_protects_and_restores_exact_registered_task_xml() -> None:
    text = _script(INSTALLER)
    rollback = _function(text, "Invoke-Rollback")
    _assert_ordered(
        text,
        "$PriorTaskXml = $PriorTask.Xml",
        "[System.IO.File]::WriteAllText($PriorTaskXmlPath",
        "Set-RestrictedAcl $PriorTaskXmlPath",
        "$MutationStarted = $true",
        'Invoke-JournaledMutation -Name "prior-task-stop"',
    )
    assert "$PriorTaskState = [int]$PriorTask.State" in text
    assert "[System.IO.File]::ReadAllText($PriorTaskXmlPath)" in rollback
    assert "$Journal.TaskRegistration.Attempted" in rollback
    assert "RegisterTask(" in rollback
    assert "$PriorTaskXmlPath" in rollback
    assert "$PriorTaskWasRunning" in text
    assert "$PriorTaskWasActive" in rollback
    assert "$PriorTaskState -eq $TaskStateRunning -or" in text
    assert "$PriorTaskState -eq $TaskStateQueued" in text
    assert "if ($PriorTaskWasPresent -and $PriorTaskWasActive)" in text


def test_installer_locks_existing_root_transactionally_before_live_replacement() -> None:
    text = _script(INSTALLER)
    live_flow = text[text.index("try {\n    $Principal") :]
    rollback = _function(text, "Invoke-Rollback")
    rollback_verification = _function(text, "Test-RollbackState")
    assert "Get-FileSystemSecuritySnapshot" in text
    assert "$Journal.InstallRoot.SecuritySnapshot" in text
    _assert_ordered(
        live_flow,
        "$Journal.InstallRoot.SecuritySnapshot",
        "$MutationStarted = $true",
        'Invoke-JournaledMutation -Name "restrict-install-root"',
        'Invoke-JournaledMutation -Name "prior-task-stop"',
        'Invoke-JournaledMutation -Name "remove-prior-file"',
    )
    assert "Restore-FileSystemSecuritySnapshot" in rollback
    assert "$Journal.InstallRoot.SecuritySnapshot" in rollback
    assert "Test-FileSystemSecuritySnapshot" in rollback_verification


def test_installer_restores_prior_managed_filesystem_security_metadata() -> None:
    text = _script(INSTALLER)
    rollback = _function(text, "Invoke-Rollback")
    rollback_verification = _function(text, "Test-RollbackState")
    assert "Get-FileSystemSecurityTree" in text
    assert "Restore-FileSystemSecurityTree" in text
    assert "Test-FileSystemSecurityTree" in text
    assert "[Security.AccessControl.AccessControlSections]::Access" in text
    assert "[Security.AccessControl.AccessControlSections]::Owner" in text
    assert "[Security.AccessControl.AccessControlSections]::Group" in text
    security_snapshot = _function(text, "Get-FileSystemSecuritySnapshot")
    assert "Get-Acl -LiteralPath $Path" in security_snapshot
    _assert_ordered(
        text,
        "$PriorState.SecuritySnapshots = Get-FileSystemSecurityTree $LivePath",
        "Copy-Item -LiteralPath $LivePath -Destination $BackupPath -Recurse -Force",
        "Set-RestrictedAcl $BackupPath",
        "$PriorState.BackupPrepared = $true",
        'Invoke-JournaledMutation -Name "remove-prior-file"',
    )
    assert (
        "Restore-FileSystemSecurityTree $PriorState.SecuritySnapshots" in rollback
    )
    assert "Set-RestrictedAcl $LivePath" not in rollback
    assert "Test-FileSystemSecurityTree $PriorState.SecuritySnapshots" in (
        rollback_verification
    )


def test_installer_journals_every_live_mutation_boundary() -> None:
    text = _script(INSTALLER)
    mutation_helper = _function(text, "Invoke-JournaledMutation")
    _assert_ordered(
        mutation_helper,
        "$State.Attempted = $true",
        "$Result = & $Action",
        "$State.Completed = $true",
    )
    boundaries = (
        "install-root",
        "restrict-install-root",
        "prior-task-stop",
        "remove-prior-file",
        "publish-file",
        "create-state-directory",
        "register-task",
        "start-task",
    )
    for boundary in boundaries:
        assert f'Invoke-JournaledMutation -Name "{boundary}"' in text


def test_transformed_real_journal_control_flow_injects_each_boundary_failure() -> None:
    text = _script(INSTALLER)
    helper = _function(text, "Invoke-JournaledMutation")
    statements: list[str] = []
    for line in helper.splitlines():
        stripped = line.strip()
        if stripped == "$State.Attempted = $true":
            statements.append("attempted")
        elif stripped == "$Result = & $Action":
            statements.append("action")
        elif stripped == "$State.Completed = $true":
            statements.append("completed")
    assert statements == ["attempted", "action", "completed"]

    boundaries = re.findall(
        r'Invoke-JournaledMutation -Name "([^"]+)"',
        text,
    )
    assert set(boundaries) == {
        "install-root",
        "restrict-install-root",
        "prior-task-stop",
        "remove-prior-file",
        "publish-file",
        "create-state-directory",
        "register-task",
        "start-task",
    }

    def run_transformed(failure_position: str) -> tuple[bool, bool, bool]:
        state = {"attempted": False, "completed": False}
        action_completed = False
        for statement in statements:
            if statement == "attempted":
                state["attempted"] = True
            elif statement == "action":
                if failure_position == "before":
                    break
                action_completed = True
                if failure_position == "after":
                    break
            elif statement == "completed":
                state["completed"] = True
        return state["attempted"], state["completed"], action_completed

    for _boundary in boundaries:
        assert run_transformed("before") == (True, False, False)
        assert run_transformed("after") == (True, False, True)
        assert run_transformed("") == (True, True, True)


def test_installer_failure_injection_rollback_uses_only_completed_resources() -> None:
    text = _script(INSTALLER)
    rollback = _function(text, "Invoke-Rollback")
    assert "$PublishedState.Completed" in rollback
    assert "$PriorState.BackupPrepared" in rollback
    assert "$PriorState.Removal.Attempted" in rollback
    assert "-State $PriorState.Removal" in text
    assert "$StateDirectoryJournal.Completed" in rollback
    assert "$InstallRootWasPresent" in text
    assert "$Journal.InstallRoot.Attempted" in rollback
    assert "$Journal.PriorTaskStop.Attempted" in rollback
    assert "$Journal.TaskRegistration.Attempted" in rollback
    assert "foreach ($Name in $ManagedNames)" in rollback
    assert not re.search(
        r"if \(Test-Path -LiteralPath \$LivePath\) \{\s*"
        r"Remove-SafePath \$LivePath\s*\}",
        rollback,
    )
    assert "Test-RollbackState" in text
    assert "Resolve-AmbiguousJournal" in text
    assert "rollback complete" in text
    assert "recovery-required at C:\\ProgramData\\.monitor-agent-recovery" in text


def test_installer_protects_each_backup_before_removing_live_data() -> None:
    text = _script(INSTALLER)
    live_flow = text[text.index("try {\n    $Principal") :]
    acl_function = _function(text, "Set-RestrictedAcl")
    assert '"/T"' in acl_function
    assert "Get-ChildItem -LiteralPath $Path -Force -Recurse" in acl_function
    assert "$Rule.IsInherited" in acl_function
    assert "$ExpectedSids -notcontains $Sid" in acl_function
    _assert_ordered(
        live_flow,
        "Copy-Item -LiteralPath $LivePath -Destination $BackupPath -Recurse -Force",
        "Set-RestrictedAcl $BackupPath",
        "$PriorState.BackupPrepared = $true",
        'Invoke-JournaledMutation -Name "remove-prior-file"',
        "Remove-SafePath $LivePath",
    )


def test_installer_requires_bounded_replacement_readiness_and_cleans_success() -> None:
    text = _script(INSTALLER)
    live_flow = text[text.index("try {\n    $Principal") :]
    readiness = _function(text, "Wait-TaskRunning")
    assert "for ($Attempt = 0; $Attempt -lt $ReadinessAttempts; $Attempt++)" in readiness
    assert "Start-Sleep -Milliseconds $ReadinessDelayMilliseconds" in readiness
    assert "[int]$RegisteredTask.State -eq $TaskStateRunning" in readiness
    assert "[int]$RegisteredTask.State -eq $TaskStateQueued" in readiness
    assert "[int]$RegisteredTask.State -eq $TaskStateReady" in readiness
    assert "$DeploymentCommitted = $true" in text
    assert "if ($DeploymentCommitted)" in text
    _assert_ordered(
        live_flow,
        '$RunningTask = Invoke-JournaledMutation -Name "start-task"',
        "Wait-TaskRunning",
        "Remove-SafePath $TransactionRoot",
        "Remove-SafePath $BackupRoot",
        "Remove-SafePath $RecoveryRoot",
        '$Succeeded = $true',
    )


def test_installer_requires_running_to_survive_the_full_observation_window() -> None:
    readiness = _function(_script(INSTALLER), "Wait-TaskRunning")
    assert "$ObservedRunning = $false" in readiness
    assert "$ObservedRunning = $true" in readiness
    assert "if (-not $ObservedRunning) { return $false }" in readiness
    assert re.search(
        r"for \(\$StabilityAttempt = 0;\s*"
        r"\$StabilityAttempt -lt \$ReadinessAttempts;\s*"
        r"\$StabilityAttempt\+\+\)",
        readiness,
    )
    stability_loop = readiness[readiness.index("for ($StabilityAttempt = 0;") :]
    assert "[int]$RegisteredTask.State -ne $TaskStateRunning" in stability_loop
    assert stability_loop.index("return $false") < stability_loop.index("return $true")


def test_uninstaller_uses_same_api_semantics_and_blocks_cleanup_on_task_failures() -> None:
    text = _script(UNINSTALLER)
    live_flow = text[text.index("try {\n    $Principal") :]
    assert "param([switch]$Purge)" in text
    assert "Administrator" in text
    assert 'New-Object -ComObject "Schedule.Service"' in text
    assert "$TaskNotFoundHResult = -2147024894" in text
    assert "$TaskStateQueued = 2" in text
    assert "$TaskStateRunning = 4" in text
    assert ".Exception.HResult -eq $TaskNotFoundHResult" in text
    assert "[int]$RegisteredTask.State -eq $TaskStateRunning -or" in text
    assert "[int]$RegisteredTask.State -eq $TaskStateQueued" in text
    assert "schtasks" not in text.lower()
    assert "Verify-TaskAbsent" in text
    assert "Remove-RuntimeArtifacts" in text
    assert "monitor-agent.env" in text
    assert "logs" in text and "spool" in text
    assert "Remove-Item -LiteralPath $InstallRoot -Recurse -Force" in text
    assert "SilentlyContinue" not in text
    assert "ReparsePoint" in text
    _assert_ordered(
        live_flow,
        "$RegisteredTask.Stop(0)",
        "$TaskFolder.DeleteTask($TaskName, 0)",
        "Verify-TaskAbsent",
        "Remove-RuntimeArtifacts",
    )
    assert 'Write-Error "monitor-agent uninstall: failed"' in text


def test_uninstaller_preflights_root_and_runtime_types_before_task_mutation() -> None:
    text = _script(UNINSTALLER)
    live_flow = text[text.index("try {\n    $Principal") :]
    preflight = _function(text, "Assert-UninstallTargetsSafe")
    assert '"venv"' in preflight
    assert '"run-agent.ps1"' in preflight
    assert '"monitor_agent_task.xml"' in preflight
    assert "$ExpectedDirectory" in text
    assert "$Item.PSIsContainer -ne $ExpectedDirectory" in text
    _assert_ordered(
        live_flow,
        "Assert-UninstallTargetsSafe",
        "Connect-TaskScheduler",
        "$RegisteredTask.Stop(0)",
        "$TaskFolder.DeleteTask($TaskName, 0)",
    )


def test_real_powershell_control_flow_handles_injected_deployment_failures() -> None:
    pwsh = shutil.which("pwsh")
    if pwsh is None:
        pytest.skip("pwsh is unavailable; transformed failure harness runs in Windows CI")

    installer = _script(INSTALLER)
    uninstaller = _script(UNINSTALLER)
    fake_acl = """function Set-RestrictedAcl {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [switch]$Recurse
    )
    if (-not (Test-Path -LiteralPath $Path)) { throw "missing ACL target" }
    $Targets = @((Get-Item -LiteralPath $Path -Force))
    if ($Recurse -and $Targets[0].PSIsContainer) {
        $Targets += @(Get-ChildItem -LiteralPath $Path -Force -Recurse)
    }
    foreach ($TargetItem in $Targets) {
        $global:SecurityState[$TargetItem.FullName] = "restricted"
    }
    Add-Content -LiteralPath $global:TaskLogPath -Value ("acl:" + $Targets[0].FullName)
}"""
    fake_security_snapshot = """function Get-FileSystemSecuritySnapshot {
    param([Parameter(Mandatory = $true)][string]$Path)
    $Item = Get-Item -LiteralPath $Path -Force
    if (-not $global:SecurityState.ContainsKey($Item.FullName)) {
        $global:SecurityState[$Item.FullName] = "prior-security"
    }
    return @{
        Path = $Item.FullName
        IsDirectory = [bool]$Item.PSIsContainer
        Sddl = $global:SecurityState[$Item.FullName]
    }
}"""
    fake_security_restore = """function Restore-FileSystemSecuritySnapshot {
    param([Parameter(Mandatory = $true)][hashtable]$Snapshot)
    $global:SecurityState[$Snapshot.Path] = $Snapshot.Sddl
    Add-Content -LiteralPath $global:TaskLogPath -Value (
        "restore-security:" + $Snapshot.Path + ":" + $Snapshot.Sddl
    )
}"""
    fake_security_test = """function Test-FileSystemSecuritySnapshot {
    param([Parameter(Mandatory = $true)][hashtable]$Snapshot)
    if (-not (Test-Path -LiteralPath $Snapshot.Path)) { return $false }
    return $global:SecurityState[$Snapshot.Path] -eq $Snapshot.Sddl
}"""
    fake_connect = """function Connect-TaskScheduler {
    return $global:FakeTaskFolder
}"""
    admin_check = """    $Principal = New-Object Security.Principal.WindowsPrincipal(
        [Security.Principal.WindowsIdentity]::GetCurrent()
    )
    if (-not $Principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        Fail "Administrator privileges required"
    }

"""
    command_check = """    if (-not (Test-PathCommand "py") -or
        -not (Test-PathCommand "schtasks.exe") -or
        -not (Test-PathCommand "icacls.exe")) {
        Fail "required Windows command is unavailable"
    }
"""
    staged_commands_start = '    py "-$PythonVersion" -m venv $StageVenv\n'
    staged_commands_end = (
        '    if ($LASTEXITCODE -ne 0) { '
        'Fail "staged configuration validation failed" }\n'
    )
    staged_replacement = """    New-Item -ItemType Directory -LiteralPath (
        Join-Path $StageVenv "Scripts"
    ) -Force | Out-Null
    [System.IO.File]::WriteAllText($StagePython, "python")
    [System.IO.File]::WriteAllText(
        (Join-Path $StageVenv "replacement.txt"),
        "replacement"
    )
"""

    with tempfile.TemporaryDirectory() as temporary_directory:
        harness_root = Path(temporary_directory)
        transformed_install = installer.replace(
            '$InstallRoot = "C:\\ProgramData\\MonitorAgent"',
            "$InstallRoot = __TEST_INSTALL_ROOT__",
        )
        transformed_install = transformed_install.replace(
            "$ScriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path",
            f"$ScriptRoot = {_ps_literal(WINDOWS)}",
        )
        transformed_install = transformed_install.replace(
            "$ProjectRoot = Split-Path -Parent (Split-Path -Parent $ScriptRoot)",
            f"$ProjectRoot = {_ps_literal(ROOT)}",
        )
        transformed_install = transformed_install.replace(admin_check, "")
        transformed_install = transformed_install.replace(command_check, "")
        transformed_install = transformed_install.replace(
            "$ReadinessAttempts = 20",
            "$ReadinessAttempts = 3",
        )
        transformed_install = transformed_install.replace(
            "$ReadinessDelayMilliseconds = 250",
            "$ReadinessDelayMilliseconds = 1",
        )
        transformed_install = _replace_function(
            transformed_install,
            "Set-RestrictedAcl",
            fake_acl,
        )
        transformed_install = _replace_function(
            transformed_install,
            "Get-FileSystemSecuritySnapshot",
            fake_security_snapshot,
        )
        transformed_install = _replace_function(
            transformed_install,
            "Restore-FileSystemSecuritySnapshot",
            fake_security_restore,
        )
        transformed_install = _replace_function(
            transformed_install,
            "Test-FileSystemSecuritySnapshot",
            fake_security_test,
        )
        transformed_install = _replace_function(
            transformed_install,
            "Connect-TaskScheduler",
            fake_connect,
        )
        remove_safe_path = _function(transformed_install, "Remove-SafePath")
        transformed_install = transformed_install.replace(
            remove_safe_path,
            remove_safe_path.replace(
                "    if (-not (Test-Path -LiteralPath $Path)) { return }",
                '    if ($global:FailureMode -eq "cleanup-fail" -and '
                '$DeploymentCommitted) { throw "injected" }\n'
                "    if (-not (Test-Path -LiteralPath $Path)) { return }",
            ),
        )
        stage_start = transformed_install.index(staged_commands_start)
        stage_end = transformed_install.index(staged_commands_end, stage_start)
        stage_end += len(staged_commands_end)
        transformed_install = (
            transformed_install[:stage_start]
            + staged_replacement
            + transformed_install[stage_end:]
        )
        transformed_install = transformed_install.replace(
            "    $State.Attempted = $true\n    $Result = & $Action",
            "    $State.Attempted = $true\n"
            "    if ($global:FailureBoundary -eq $Name -and "
            '$global:FailurePosition -eq "before") { throw "injected" }\n'
            "    $Result = & $Action\n"
            "    if ($global:FailureBoundary -eq $Name -and "
            '$global:FailurePosition -eq "after") { throw "injected" }',
        )

        transformed_uninstall = uninstaller.replace(
            '$InstallRoot = "C:\\ProgramData\\MonitorAgent"',
            "$InstallRoot = __TEST_INSTALL_ROOT__",
        )
        transformed_uninstall = transformed_uninstall.replace(admin_check, "")
        transformed_uninstall = _replace_function(
            transformed_uninstall,
            "Connect-TaskScheduler",
            fake_connect,
        )

        wrapper = harness_root / "wrapper.ps1"
        wrapper.write_text(
            """param(
    [string]$Target,
    [string]$Wheel,
    [string]$EnvironmentFile,
    [string]$FailureBoundary,
    [string]$FailurePosition,
    [string]$FailureMode,
    [string]$TaskLog,
    [string]$PriorTaskXml,
    [string]$PriorTaskSddl
)
$ErrorActionPreference = "Stop"
$global:FailureBoundary = $FailureBoundary
$global:FailurePosition = $FailurePosition
$global:FailureMode = $FailureMode
$global:TaskLogPath = $TaskLog
$global:PartialRegistrationUsed = $false
$global:LateCrashUsed = $false
$global:ObserveReadiness = $false
$global:ReadinessSequence = @()
$global:SecurityState = @{}

function New-FakeTask {
    param([string]$Xml, [int]$State, [string]$Sddl)
    $Task = [pscustomobject]@{ Xml = $Xml; State = $State; Sddl = $Sddl }
    $Task | Add-Member -MemberType ScriptMethod -Name GetSecurityDescriptor -Value {
        param($SecurityInformation)
        return $this.Sddl
    }
    $Task | Add-Member -MemberType ScriptMethod -Name Stop -Value {
        param($Flags)
        Add-Content -LiteralPath $global:TaskLogPath -Value "stop"
        if ($global:FailureMode -eq "stop-fail") { throw "injected" }
        $this.State = 3
    }
    $Task | Add-Member -MemberType ScriptMethod -Name Run -Value {
        param($Parameters)
        Add-Content -LiteralPath $global:TaskLogPath -Value "run"
        if (($global:FailureMode -eq "late-crash" -or
                $global:FailureMode -eq "late-running") -and
            -not $global:LateCrashUsed) {
            $global:LateCrashUsed = $true
            $global:ObserveReadiness = $true
            if ($global:FailureMode -eq "late-running") {
                $global:ReadinessSequence = @(3, 3, 4, 3)
            }
            else {
                $global:ReadinessSequence = @(4, 4, 3)
            }
            $this.State = 4
        }
        elseif ($global:FailureBoundary -eq "readiness") {
            $this.State = 3
        }
        else {
            $this.State = 4
        }
        return $this
    }
    return $Task
}

$InitialTask = $null
if (-not [string]::IsNullOrEmpty($PriorTaskXml)) {
    $InitialTask = New-FakeTask `
        -Xml $PriorTaskXml `
        -State 4 `
        -Sddl $PriorTaskSddl
}
$Folder = [pscustomobject]@{
    Task = $InitialTask
}
$Folder | Add-Member -MemberType ScriptMethod -Name GetTask -Value {
    param($Name)
    if ($null -eq $this.Task) {
        if ($global:FailureMode -eq "query-fail") {
            throw [Runtime.InteropServices.COMException]::new("query", -1)
        }
        throw [Runtime.InteropServices.COMException]::new("absent", -2147024894)
    }
    if ($global:ObserveReadiness -and $global:ReadinessSequence.Count -gt 0) {
        $this.Task.State = [int]$global:ReadinessSequence[0]
        if ($global:ReadinessSequence.Count -eq 1) {
            $global:ReadinessSequence = @()
        }
        else {
            $global:ReadinessSequence = @(
                $global:ReadinessSequence[1..($global:ReadinessSequence.Count - 1)]
            )
        }
    }
    return $this.Task
}
$Folder | Add-Member -MemberType ScriptMethod -Name DeleteTask -Value {
    param($Name, $Flags)
    Add-Content -LiteralPath $global:TaskLogPath -Value "delete"
    if ($global:FailureMode -eq "delete-fail") { throw "injected" }
    $this.Task = $null
}
$Folder | Add-Member -MemberType ScriptMethod -Name RegisterTask -Value {
    param($Name, $Xml, $Flags, $User, $Password, $LogonType, $Sddl)
    $Encoded = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($Xml))
    $EncodedSddl = [Convert]::ToBase64String(
        [Text.Encoding]::UTF8.GetBytes([string]$Sddl)
    )
    Add-Content -LiteralPath $global:TaskLogPath -Value ("register:" + $Encoded)
    Add-Content -LiteralPath $global:TaskLogPath -Value ("register-sddl:" + $EncodedSddl)
    $EffectiveSddl = $Sddl
    if ($null -eq $EffectiveSddl) { $EffectiveSddl = "replacement-default" }
    $this.Task = New-FakeTask -Xml $Xml -State 3 -Sddl $EffectiveSddl
    $global:ObserveReadiness = $false
    if ($global:FailureMode -eq "register-partial" -and
        -not $global:PartialRegistrationUsed) {
        $global:PartialRegistrationUsed = $true
        throw "injected"
    }
    return $this.Task
}
$global:FakeTaskFolder = $Folder

if ($Wheel) {
    & $Target -WheelPath $Wheel -EnvironmentFile $EnvironmentFile
}
else {
    & $Target
}
""",
            encoding="utf-8",
        )

        prior_task_xml = "<Task><RegistrationInfo>registered-prior</RegistrationInfo></Task>"
        prior_task_b64 = base64.b64encode(prior_task_xml.encode()).decode()
        prior_task_sddl = "O:SYG:SYD:P(A;;FA;;;SY)(A;;FA;;;BA)"
        prior_task_sddl_b64 = base64.b64encode(prior_task_sddl.encode()).decode()

        def prepare_root(case_root: Path, *, present: bool = True) -> None:
            if not present:
                return
            (case_root / "venv").mkdir(parents=True)
            (case_root / "venv" / "prior.txt").write_text("prior", encoding="utf-8")
            (case_root / "monitor-agent.env").write_text(
                "prior-config",
                encoding="utf-8",
            )
            (case_root / "run-agent.ps1").write_text("prior", encoding="utf-8")
            (case_root / "monitor_agent_task.xml").write_text(
                "<Task>disk-prior</Task>",
                encoding="utf-8",
            )
            for state_name in ("logs", "spool"):
                state_path = case_root / state_name
                state_path.mkdir()
                (state_path / "preserved.txt").write_text("state", encoding="utf-8")

        def run_installer(
            boundary: str,
            *,
            position: str = "before",
            mode: str = "",
            root_present: bool = True,
            prior_task_present: bool = True,
        ) -> tuple[subprocess.CompletedProcess[str], Path, str]:
            case_name = "-".join(
                part
                for part in (
                    boundary or "success",
                    position,
                    mode,
                    "" if prior_task_present else "task-absent",
                )
                if part
            )
            case_dir = harness_root / ("install-" + case_name)
            case_dir.mkdir()
            install_root = case_dir / "MonitorAgent"
            prepare_root(install_root, present=root_present)
            wheel = case_dir / "monitor_agent.whl"
            wheel.write_bytes(b"wheel")
            environment = case_dir / "monitor-agent.env"
            environment.write_text(
                "MONITOR_API_TOKEN=never-print-this-value\n",
                encoding="utf-8",
            )
            log = case_dir / "task.log"
            script = case_dir / "install.ps1"
            script.write_text(
                transformed_install.replace(
                    "__TEST_INSTALL_ROOT__",
                    _ps_literal(install_root),
                ),
                encoding="utf-8",
            )
            result = subprocess.run(
                [
                    pwsh,
                    "-NoProfile",
                    "-File",
                    str(wrapper),
                    "-Target",
                    str(script),
                    "-Wheel",
                    str(wheel),
                    "-EnvironmentFile",
                    str(environment),
                    "-FailureBoundary",
                    boundary,
                    "-FailurePosition",
                    position,
                    "-FailureMode",
                    mode,
                    "-TaskLog",
                    str(log),
                    "-PriorTaskXml",
                    prior_task_xml if prior_task_present else "",
                    "-PriorTaskSddl",
                    prior_task_sddl if prior_task_present else "",
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            task_log = log.read_text(encoding="utf-8") if log.exists() else ""
            return result, install_root, task_log

        journal_boundaries = (
            ("install-root", False),
            ("restrict-install-root", True),
            ("prior-task-stop", True),
            ("remove-prior-file", True),
            ("publish-file", True),
            ("create-state-directory", True),
            ("register-task", True),
            ("start-task", True),
        )
        failure_cases = [
            (boundary, position, "", root_present)
            for boundary, root_present in journal_boundaries
            for position in ("before", "after")
        ]
        failure_cases.extend(
            [
                ("", "before", "register-partial", True),
                ("readiness", "before", "", True),
                ("", "before", "late-crash", True),
                ("", "before", "late-running", True),
            ]
        )
        for boundary, position, mode, root_present in failure_cases:
            result, install_root, task_log = run_installer(
                boundary,
                position=position,
                mode=mode,
                root_present=root_present,
            )
            assert result.returncode != 0, (
                boundary,
                position,
                mode,
                result.stdout,
                result.stderr,
            )
            assert "never-print-this-value" not in result.stdout + result.stderr
            assert not (install_root.parent / ".monitor-agent-recovery").exists()
            if root_present:
                assert (install_root / "venv" / "prior.txt").read_text() == "prior"
                assert not (install_root / "venv" / "replacement.txt").exists()
                assert (install_root / "logs" / "preserved.txt").exists()
                assert (install_root / "spool" / "preserved.txt").exists()
            else:
                assert not install_root.exists()
            if boundary in {"register-task", "start-task", "readiness"} or mode:
                assert f"register:{prior_task_b64}" in task_log
                assert f"register-sddl:{prior_task_sddl_b64}" in task_log
            if root_present:
                assert (
                    f"restore-security:{install_root}:prior-security" in task_log
                )

        absent_failure, absent_root, absent_log = run_installer(
            "start-task",
            prior_task_present=False,
        )
        assert absent_failure.returncode != 0
        assert (absent_root / "venv" / "prior.txt").exists()
        assert absent_log.count("register:") == 1
        assert f"register-sddl:{prior_task_sddl_b64}" not in absent_log

        cleanup_failed, cleanup_root, cleanup_log = run_installer(
            "",
            position="",
            mode="cleanup-fail",
        )
        assert cleanup_failed.returncode != 0
        assert (cleanup_root / "venv" / "replacement.txt").exists()
        assert (cleanup_root.parent / ".monitor-agent-recovery").exists()
        assert cleanup_log.count(f"register:{prior_task_b64}") == 0

        success, success_root, success_log = run_installer("", position="")
        assert success.returncode == 0, success.stderr or success.stdout
        assert (success_root / "venv" / "replacement.txt").exists()
        assert not (success_root.parent / ".monitor-agent-recovery").exists()
        success_events = success_log.splitlines()
        assert success_events.index(f"acl:{success_root}") < success_events.index("stop")

        def run_uninstaller(
            mode: str,
            *,
            unsafe_name: str = "",
        ) -> tuple[subprocess.CompletedProcess[str], Path, str]:
            case_name = "-".join(
                part for part in (mode or "success", unsafe_name) if part
            )
            case_dir = harness_root / ("uninstall-" + case_name)
            case_dir.mkdir()
            install_root = case_dir / "MonitorAgent"
            prepare_root(install_root)
            if unsafe_name == "venv":
                shutil.rmtree(install_root / "venv")
                (install_root / "venv").write_text("unsafe-file", encoding="utf-8")
            elif unsafe_name == "run-agent.ps1":
                (install_root / "run-agent.ps1").unlink()
                (install_root / "run-agent.ps1").mkdir()
            log = case_dir / "task.log"
            script = case_dir / "uninstall.ps1"
            script.write_text(
                transformed_uninstall.replace(
                    "__TEST_INSTALL_ROOT__",
                    _ps_literal(install_root),
                ),
                encoding="utf-8",
            )
            result = subprocess.run(
                [
                    pwsh,
                    "-NoProfile",
                    "-File",
                    str(wrapper),
                    "-Target",
                    str(script),
                    "-FailureMode",
                    mode,
                    "-TaskLog",
                    str(log),
                    "-PriorTaskXml",
                    prior_task_xml,
                    "-PriorTaskSddl",
                    prior_task_sddl,
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            task_log = log.read_text(encoding="utf-8") if log.exists() else ""
            return result, install_root, task_log

        for mode in ("stop-fail", "delete-fail", "query-fail"):
            failed, failed_root, _failed_log = run_uninstaller(mode)
            assert failed.returncode != 0
            assert (failed_root / "venv" / "prior.txt").exists()

        for unsafe_name in ("venv", "run-agent.ps1"):
            unsafe, unsafe_root, unsafe_log = run_uninstaller(
                "",
                unsafe_name=unsafe_name,
            )
            assert unsafe.returncode != 0
            assert unsafe_log == ""
            assert unsafe_root.exists()

        removed, removed_root, _removed_log = run_uninstaller("")
        assert removed.returncode == 0, removed.stderr or removed.stdout
        assert not (removed_root / "venv").exists()
        assert (removed_root / "monitor-agent.env").exists()
        assert (removed_root / "logs" / "preserved.txt").exists()
        assert (removed_root / "spool" / "preserved.txt").exists()


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
