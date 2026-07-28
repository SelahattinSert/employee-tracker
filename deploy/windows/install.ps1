param(
    [Parameter(Mandatory = $true)]
    [string]$WheelPath,
    [Parameter(Mandatory = $true)]
    [string]$EnvironmentFile,
    [ValidateSet("3.11", "3.12", "3.13", "3.14")]
    [string]$PythonVersion = "3.11"
)

$ErrorActionPreference = "Stop"
$TaskName = "MonitorAgent"
$InstallRoot = "C:\ProgramData\MonitorAgent"
$InstallParent = Split-Path -Parent $InstallRoot
$ScriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Split-Path -Parent (Split-Path -Parent $ScriptRoot)
$LockPath = Join-Path $ProjectRoot "requirements.lock"
$LauncherSource = Join-Path $ScriptRoot "run-agent.ps1"
$TaskSource = Join-Path $ScriptRoot "monitor_agent_task.xml"

function Fail {
    param([Parameter(Mandatory = $true)][string]$Message)
    throw "monitor-agent install: $Message"
}

function Test-ReparsePoint {
    param([Parameter(Mandatory = $true)][System.IO.FileSystemInfo]$Item)
    return (($Item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0)
}

function Test-RegularFile {
    param([Parameter(Mandatory = $true)][string]$Path)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $false }
    return -not (Test-ReparsePoint (Get-Item -LiteralPath $Path -Force))
}

function Assert-RegularFile {
    param([Parameter(Mandatory = $true)][string]$Path, [string]$Label = "file")
    if (-not (Test-RegularFile $Path)) { Fail "required $Label is missing or unsafe" }
}

function Assert-SafeDirectory {
    param([Parameter(Mandatory = $true)][string]$Path, [string]$Label = "directory")
    if (-not (Test-Path -LiteralPath $Path)) { return }
    $Item = Get-Item -LiteralPath $Path -Force
    if (-not $Item.PSIsContainer -or (Test-ReparsePoint $Item)) {
        Fail "$Label is not a safe directory"
    }
}

function Test-PathCommand {
    param([Parameter(Mandatory = $true)][string]$Name)
    return $null -ne (Get-Command $Name -CommandType Application -ErrorAction SilentlyContinue)
}

function Set-RestrictedAcl {
    param([Parameter(Mandatory = $true)][string]$Path)
    $AclArguments = @(
        "/reset",
        "/inheritance:r",
        "/grant:r",
        "*S-1-5-18:(OI)(CI)F",
        "*S-1-5-32-544:(OI)(CI)F"
    )
    & icacls.exe $Path @AclArguments | Out-Null
    if ($LASTEXITCODE -ne 0) { Fail "ACL configuration failed" }
    $ExpectedSids = @("S-1-5-18", "S-1-5-32-544")
    $ObservedSids = New-Object -TypeName "System.Collections.Generic.HashSet[string]"
    foreach ($Rule in (Get-Acl -LiteralPath $Path).Access) {
        $Sid = $Rule.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value
        $HasFullControl = (($Rule.FileSystemRights -band [Security.AccessControl.FileSystemRights]::FullControl) -eq [Security.AccessControl.FileSystemRights]::FullControl)
        if ($Rule.IsInherited -or $ExpectedSids -notcontains $Sid -or
            $Rule.AccessControlType -ne [Security.AccessControl.AccessControlType]::Allow -or
            -not $HasFullControl) {
            Fail "DACL verification failed"
        }
        [void]$ObservedSids.Add($Sid)
    }
    foreach ($ExpectedSid in $ExpectedSids) {
        if (-not $ObservedSids.Contains($ExpectedSid)) { Fail "DACL verification failed" }
    }
}

function Get-TaskNotFound {
    param([Parameter(Mandatory = $true)][string]$Output)
    return $Output -match "(?i)(cannot find|does not exist)"
}

function Test-TaskExists {
    $TaskOutput = (& schtasks.exe /Query /TN MonitorAgent 2>&1 | Out-String)
    $TaskExitCode = $LASTEXITCODE
    if ($TaskExitCode -eq 0) { return $true }
    if ($TaskExitCode -eq 1 -and (Get-TaskNotFound $TaskOutput)) { return $false }
    Fail "unable to query task"
}

function Remove-SafePath {
    param([Parameter(Mandatory = $true)][string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) { return }
    $Item = Get-Item -LiteralPath $Path -Force
    if (Test-ReparsePoint $Item) { Fail "refusing to remove reparse point" }
    Remove-Item -LiteralPath $Path -Recurse -Force
}

function Invoke-Rollback {
    $RollbackFailed = $false
    try {
        if (Test-TaskExists) {
            & schtasks.exe /Delete /TN MonitorAgent /F | Out-Null
            if ($LASTEXITCODE -ne 0) { throw "task deletion failed" }
            if (Test-TaskExists) { throw "task deletion verification failed" }
        }
    }
    catch { $RollbackFailed = $true }

    foreach ($Name in $ManagedNames) {
        try {
            $LivePath = Join-Path $InstallRoot $Name
            if (Test-Path -LiteralPath $LivePath) { Remove-SafePath $LivePath }
            $BackupPath = Join-Path $BackupRoot $Name
            if (Test-Path -LiteralPath $BackupPath) {
                Move-Item -LiteralPath $BackupPath -Destination $LivePath
            }
        }
        catch { $RollbackFailed = $true }
    }
    if (-not $InstallRootWasPresent) {
        try { Remove-SafePath $InstallRoot }
        catch { $RollbackFailed = $true }
    }

    if ($TaskWasPresent) {
        try {
            $BackupTask = Join-Path $InstallRoot "monitor_agent_task.xml"
            if (-not (Test-RegularFile $BackupTask)) { throw "backup task is absent" }
            & schtasks.exe /Create /TN MonitorAgent /XML $BackupTask /F | Out-Null
            if ($LASTEXITCODE -ne 0 -or -not (Test-TaskExists)) {
                throw "task restoration failed"
            }
            if ($TaskWasRunning) {
                & schtasks.exe /Run /TN MonitorAgent | Out-Null
                if ($LASTEXITCODE -ne 0) { throw "task restart failed" }
                $RestoredTaskDetails = (& schtasks.exe /Query /TN MonitorAgent /FO LIST /V 2>&1 | Out-String)
                if ($LASTEXITCODE -ne 0 -or $RestoredTaskDetails -notmatch "Running") {
                    throw "task state verification failed"
                }
            }
        }
        catch { $RollbackFailed = $true }
    }
    return -not $RollbackFailed
}

$Principal = New-Object Security.Principal.WindowsPrincipal(
    [Security.Principal.WindowsIdentity]::GetCurrent()
)
if (-not $Principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Fail "Administrator privileges required"
}

Assert-RegularFile $WheelPath "wheel"
if ([System.IO.Path]::GetExtension($WheelPath) -ine ".whl") {
    Fail "wheel must have a .whl suffix"
}
Assert-RegularFile $EnvironmentFile "environment file"
Assert-RegularFile $LockPath "lock file"
Assert-RegularFile $LauncherSource "launcher template"
Assert-RegularFile $TaskSource "task template"
if (-not (Test-PathCommand "py") -or -not (Test-PathCommand "schtasks.exe") -or
    -not (Test-PathCommand "icacls.exe")) {
    Fail "required Windows command is unavailable"
}
Assert-SafeDirectory $InstallParent "install parent"
Assert-SafeDirectory $InstallRoot "install root"

$TransactionRoot = Join-Path $InstallParent (".monitor-agent-transaction-" + [guid]::NewGuid())
$BackupRoot = Join-Path $InstallParent (".monitor-agent-rollback-" + [guid]::NewGuid())
$StageVenv = Join-Path $TransactionRoot "venv"
$StageConfig = Join-Path $TransactionRoot "monitor-agent.env"
$StageLauncher = Join-Path $TransactionRoot "run-agent.ps1"
$StageTask = Join-Path $TransactionRoot "monitor_agent_task.xml"
$StageLock = Join-Path $TransactionRoot "requirements.lock"
$StageWheel = Join-Path $TransactionRoot (Split-Path -Leaf $WheelPath)
$StagePython = Join-Path $StageVenv "Scripts\python.exe"
$ManagedNames = @("venv", "monitor-agent.env", "run-agent.ps1", "monitor_agent_task.xml")
$MutationStarted = $false
$Succeeded = $false
$TaskWasPresent = $false
$TaskWasRunning = $false
$InstallRootWasPresent = Test-Path -LiteralPath $InstallRoot

try {
    New-Item -ItemType Directory -LiteralPath $TransactionRoot -Force | Out-Null
    Set-RestrictedAcl $TransactionRoot
    Copy-Item -LiteralPath $EnvironmentFile -Destination $StageConfig -Force
    Copy-Item -LiteralPath $LauncherSource -Destination $StageLauncher -Force
    Copy-Item -LiteralPath $TaskSource -Destination $StageTask -Force
    Copy-Item -LiteralPath $LockPath -Destination $StageLock -Force
    Copy-Item -LiteralPath $WheelPath -Destination $StageWheel -Force

    py "-$PythonVersion" -m venv $StageVenv
    if ($LASTEXITCODE -ne 0) { Fail "virtual environment creation failed" }
    Assert-RegularFile $StagePython "staged Python executable"
    & $StagePython -m pip install --require-hashes -r $StageLock
    if ($LASTEXITCODE -ne 0) { Fail "locked dependency installation failed" }
    & $StagePython -m pip install --no-deps --force-reinstall $StageWheel
    if ($LASTEXITCODE -ne 0) { Fail "wheel installation failed" }
    & $StageLauncher -Command check-config -InstallRoot $TransactionRoot
    if ($LASTEXITCODE -ne 0) { Fail "staged configuration validation failed" }

    $TaskWasPresent = Test-TaskExists
    if ($TaskWasPresent) {
        $TaskDetails = (& schtasks.exe /Query /TN $TaskName /FO LIST /V 2>$null | Out-String)
        if ($LASTEXITCODE -ne 0) { Fail "unable to inspect existing task" }
        $TaskWasRunning = $TaskDetails -match "Running"
    }

    $MutationStarted = $true
    New-Item -ItemType Directory -LiteralPath $BackupRoot -Force | Out-Null
    Set-RestrictedAcl $BackupRoot
    if (-not (Test-Path -LiteralPath $InstallRoot)) {
        New-Item -ItemType Directory -LiteralPath $InstallRoot -Force | Out-Null
    }
    Assert-SafeDirectory $InstallRoot "install root"
    if ($TaskWasPresent -and $TaskWasRunning) {
        & schtasks.exe /End /TN $TaskName | Out-Null
        if ($LASTEXITCODE -ne 0) { Fail "unable to stop existing task" }
    }
    foreach ($Name in $ManagedNames) {
        $LivePath = Join-Path $InstallRoot $Name
        if (Test-Path -LiteralPath $LivePath) {
            $LiveItem = Get-Item -LiteralPath $LivePath -Force
            if (Test-ReparsePoint $LiveItem) { Fail "managed target is unsafe" }
            Move-Item -LiteralPath $LivePath -Destination (Join-Path $BackupRoot $Name)
        }
    }
    foreach ($Name in $ManagedNames) {
        Move-Item -LiteralPath (Join-Path $TransactionRoot $Name) -Destination (Join-Path $InstallRoot $Name)
    }
    foreach ($StateDirectory in @("logs", "spool")) {
        $StatePath = Join-Path $InstallRoot $StateDirectory
        Assert-SafeDirectory $StatePath $StateDirectory
        if (-not (Test-Path -LiteralPath $StatePath)) {
            New-Item -ItemType Directory -LiteralPath $StatePath -Force | Out-Null
        }
    }
    Set-RestrictedAcl $InstallRoot
    foreach ($Name in $ManagedNames) { Set-RestrictedAcl (Join-Path $InstallRoot $Name) }

    & schtasks.exe /Create /TN MonitorAgent /XML (Join-Path $InstallRoot "monitor_agent_task.xml") /F | Out-Null
    if ($LASTEXITCODE -ne 0) { Fail "task registration failed" }
    & schtasks.exe /Run /TN MonitorAgent | Out-Null
    if ($LASTEXITCODE -ne 0) { Fail "task start failed" }
    if (-not (Test-TaskExists)) { Fail "task registration verification failed" }
    $Succeeded = $true
}
catch {
    if ($MutationStarted) {
        $null = Invoke-Rollback
    }
    throw "monitor-agent install: deployment failed; recovery artifacts retained"
}
finally {
    if ($Succeeded -and (Test-Path -LiteralPath $TransactionRoot)) {
        Remove-SafePath $TransactionRoot
    }
    if ($Succeeded -and (Test-Path -LiteralPath $BackupRoot)) {
        Remove-SafePath $BackupRoot
    }
}

if ($Succeeded) { Write-Host "monitor-agent install: task deployed" }
