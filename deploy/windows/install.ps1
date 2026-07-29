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
$RecoveryRoot = Join-Path $InstallParent ".monitor-agent-recovery"
$TransactionRoot = Join-Path $RecoveryRoot "transaction"
$BackupRoot = Join-Path $RecoveryRoot "backup"
$PriorTaskXmlPath = Join-Path $BackupRoot "registered-task.xml"
$ScriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Split-Path -Parent (Split-Path -Parent $ScriptRoot)
$LockPath = Join-Path $ProjectRoot "requirements.lock"
$LauncherSource = Join-Path $ScriptRoot "run-agent.ps1"
$TaskSource = Join-Path $ScriptRoot "monitor_agent_task.xml"
$TaskNotFoundHResult = -2147024894
$TaskStateQueued = 2
$TaskStateReady = 3
$TaskStateRunning = 4
$TaskCreateOrUpdate = 6
$TaskDontAddPrincipalAce = 0x10
$TaskRestoreFlags = ($TaskCreateOrUpdate -bor $TaskDontAddPrincipalAce)
$TaskLogonServiceAccount = 5
$TaskSecurityOwner = 0x1
$TaskSecurityGroup = 0x2
$TaskSecurityDacl = 0x4
$TaskSecurityInformation = (
    $TaskSecurityOwner -bor $TaskSecurityGroup -bor $TaskSecurityDacl
)
$FileSystemSecuritySections = (
    [Security.AccessControl.AccessControlSections]::Access -bor
    [Security.AccessControl.AccessControlSections]::Owner -bor
    [Security.AccessControl.AccessControlSections]::Group
)
$ReadinessAttempts = 20
$ReadinessDelayMilliseconds = 250
$ManagedNames = @("venv", "monitor-agent.env", "run-agent.ps1", "monitor_agent_task.xml")
$FailureCategory = "preflight"
$MutationStarted = $false
$DeploymentCommitted = $false
$Succeeded = $false
$InstallRootWasPresent = $false
$PriorTask = $null
$PriorTaskXml = $null
$PriorTaskSddl = $null
$PriorTaskState = 0
$PriorTaskWasPresent = $false
$PriorTaskWasRunning = $false
$PriorTaskWasActive = $false
$TaskFolder = $null

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

function Assert-SafeManagedPath {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Name
    )
    if (-not (Test-Path -LiteralPath $Path)) { return }
    $Item = Get-Item -LiteralPath $Path -Force
    if (Test-ReparsePoint $Item) { Fail "managed target is unsafe" }
    if ($Name -eq "venv" -and -not $Item.PSIsContainer) {
        Fail "managed target type is unsafe"
    }
    if ($Name -ne "venv" -and $Item.PSIsContainer) {
        Fail "managed target type is unsafe"
    }
}

function Test-PathCommand {
    param([Parameter(Mandatory = $true)][string]$Name)
    return $null -ne (Get-Command $Name -CommandType Application -ErrorAction SilentlyContinue)
}

function Set-RestrictedAcl {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [switch]$Recurse
    )
    $AclArguments = @(
        "/reset",
        "/inheritance:r",
        "/grant:r",
        "*S-1-5-18:(OI)(CI)F",
        "*S-1-5-32-544:(OI)(CI)F"
    )
    if ($Recurse) { $AclArguments += @("/T", "/C") }
    & icacls.exe $Path @AclArguments | Out-Null
    if ($LASTEXITCODE -ne 0) { Fail "ACL configuration failed" }

    $ExpectedSids = @("S-1-5-18", "S-1-5-32-544")
    $AclTargets = @((Get-Item -LiteralPath $Path -Force))
    if ($Recurse -and $AclTargets[0].PSIsContainer) {
        $AclTargets += @(Get-ChildItem -LiteralPath $Path -Force -Recurse)
    }
    foreach ($AclTarget in $AclTargets) {
        $ObservedAcl = Get-Acl -LiteralPath $AclTarget.FullName
        if (-not $ObservedAcl.AreAccessRulesProtected) {
            Fail "DACL verification failed"
        }
        $ObservedSids = New-Object -TypeName "System.Collections.Generic.HashSet[string]"
        foreach ($Rule in $ObservedAcl.Access) {
            $Sid = $Rule.IdentityReference.Translate(
                [Security.Principal.SecurityIdentifier]
            ).Value
            $HasFullControl = (
                ($Rule.FileSystemRights -band
                    [Security.AccessControl.FileSystemRights]::FullControl) -eq
                [Security.AccessControl.FileSystemRights]::FullControl
            )
            if ($Rule.IsInherited -or $ExpectedSids -notcontains $Sid -or
                $Rule.AccessControlType -ne
                    [Security.AccessControl.AccessControlType]::Allow -or
                -not $HasFullControl) {
                Fail "DACL verification failed"
            }
            [void]$ObservedSids.Add($Sid)
        }
        foreach ($ExpectedSid in $ExpectedSids) {
            if (-not $ObservedSids.Contains($ExpectedSid)) {
                Fail "DACL verification failed"
            }
        }
    }
}

function Get-FileSystemSecuritySnapshot {
    param([Parameter(Mandatory = $true)][string]$Path)
    $Item = Get-Item -LiteralPath $Path -Force
    $Acl = Get-Acl -LiteralPath $Path
    return @{
        Path = $Item.FullName
        IsDirectory = [bool]$Item.PSIsContainer
        Sddl = $Acl.GetSecurityDescriptorSddlForm($FileSystemSecuritySections)
    }
}

function Get-FileSystemSecurityTree {
    param([Parameter(Mandatory = $true)][string]$Path)
    $Targets = @((Get-Item -LiteralPath $Path -Force))
    if ($Targets[0].PSIsContainer) {
        $Targets += @(Get-ChildItem -LiteralPath $Path -Force -Recurse)
    }
    $Snapshots = @()
    foreach ($Target in $Targets) {
        $Snapshots += @(Get-FileSystemSecuritySnapshot $Target.FullName)
    }
    return ,$Snapshots
}

function Restore-FileSystemSecuritySnapshot {
    param([Parameter(Mandatory = $true)][hashtable]$Snapshot)
    $Item = Get-Item -LiteralPath $Snapshot.Path -Force
    if ([bool]$Item.PSIsContainer -ne [bool]$Snapshot.IsDirectory) {
        throw "filesystem security target type changed"
    }
    $Acl = Get-Acl -LiteralPath $Snapshot.Path
    $Acl.SetSecurityDescriptorSddlForm(
        $Snapshot.Sddl,
        $FileSystemSecuritySections
    )
    Set-Acl -LiteralPath $Snapshot.Path -AclObject $Acl
}

function Restore-FileSystemSecurityTree {
    param([Parameter(Mandatory = $true)][object[]]$Snapshots)
    $DeepestFirst = @($Snapshots | Sort-Object { $_.Path.Length } -Descending)
    foreach ($Snapshot in $DeepestFirst) {
        Restore-FileSystemSecuritySnapshot $Snapshot
    }
}

function Test-FileSystemSecuritySnapshot {
    param([Parameter(Mandatory = $true)][hashtable]$Snapshot)
    if (-not (Test-Path -LiteralPath $Snapshot.Path)) { return $false }
    $Item = Get-Item -LiteralPath $Snapshot.Path -Force
    if ([bool]$Item.PSIsContainer -ne [bool]$Snapshot.IsDirectory) {
        return $false
    }
    $Acl = Get-Acl -LiteralPath $Snapshot.Path
    return (
        $Acl.GetSecurityDescriptorSddlForm($FileSystemSecuritySections) -eq
        $Snapshot.Sddl
    )
}

function Test-FileSystemSecurityTree {
    param([Parameter(Mandatory = $true)][object[]]$Snapshots)
    foreach ($Snapshot in $Snapshots) {
        if (-not (Test-FileSystemSecuritySnapshot $Snapshot)) { return $false }
    }
    return $true
}

function Connect-TaskScheduler {
    try {
        $TaskService = New-Object -ComObject "Schedule.Service"
        $TaskService.Connect()
        return $TaskService.GetFolder("\")
    }
    catch {
        Fail "task scheduler connection failed"
    }
}

function Get-RegisteredTask {
    param(
        [Parameter(Mandatory = $true)]$Folder,
        [Parameter(Mandatory = $true)][string]$Name
    )
    try {
        return $Folder.GetTask($Name)
    }
    catch [Runtime.InteropServices.COMException] {
        if ($_.Exception.HResult -eq $TaskNotFoundHResult) { return $null }
        Fail "task query failed"
    }
    catch {
        Fail "task query failed"
    }
}

function Wait-TaskRunning {
    param(
        [Parameter(Mandatory = $true)]$Folder,
        [Parameter(Mandatory = $true)][string]$Name
    )
    $ObservedRunning = $false
    for ($Attempt = 0; $Attempt -lt $ReadinessAttempts; $Attempt++) {
        Start-Sleep -Milliseconds $ReadinessDelayMilliseconds
        $RegisteredTask = Get-RegisteredTask -Folder $Folder -Name $Name
        if ($null -eq $RegisteredTask) { return $false }
        if ([int]$RegisteredTask.State -eq $TaskStateRunning) {
            $ObservedRunning = $true
            break
        }
        if ([int]$RegisteredTask.State -eq $TaskStateQueued -or
            [int]$RegisteredTask.State -eq $TaskStateReady) {
            continue
        }
        return $false
    }
    if (-not $ObservedRunning) { return $false }

    for ($StabilityAttempt = 0; $StabilityAttempt -lt $ReadinessAttempts;
        $StabilityAttempt++) {
        Start-Sleep -Milliseconds $ReadinessDelayMilliseconds
        $RegisteredTask = Get-RegisteredTask -Folder $Folder -Name $Name
        if ($null -eq $RegisteredTask -or
            [int]$RegisteredTask.State -ne $TaskStateRunning) {
            return $false
        }
    }
    return $true
}

function Remove-SafePath {
    param([Parameter(Mandatory = $true)][string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) { return }
    $Item = Get-Item -LiteralPath $Path -Force
    if (Test-ReparsePoint $Item) { Fail "refusing to remove reparse point" }
    Remove-Item -LiteralPath $Path -Recurse -Force
}

function Restore-BackupPath {
    param(
        [Parameter(Mandatory = $true)][string]$Source,
        [Parameter(Mandatory = $true)][string]$Destination
    )
    $SourceItem = Get-Item -LiteralPath $Source -Force
    if (-not $SourceItem.PSIsContainer) {
        Copy-Item -LiteralPath $Source -Destination $Destination -Force
        return
    }
    if (-not (Test-Path -LiteralPath $Destination)) {
        New-Item -ItemType Directory -LiteralPath $Destination -Force | Out-Null
    }
    Assert-SafeDirectory $Destination "rollback destination"
    foreach ($Child in (Get-ChildItem -LiteralPath $Source -Force)) {
        Restore-BackupPath `
            -Source $Child.FullName `
            -Destination (Join-Path $Destination $Child.Name)
    }
}

function Invoke-JournaledMutation {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][hashtable]$State,
        [Parameter(Mandatory = $true)][scriptblock]$Action
    )
    $State.Attempted = $true
    $Result = & $Action
    $State.Completed = $true
    return $Result
}

function Resolve-AmbiguousJournal {
    if (-not $InstallRootWasPresent -and $Journal.InstallRoot.Attempted -and
        -not $Journal.InstallRoot.Completed -and
        (Test-Path -LiteralPath $InstallRoot)) {
        $Journal.InstallRoot.Completed = $true
    }
    foreach ($Name in $ManagedNames) {
        $LivePath = Join-Path $InstallRoot $Name
        $PriorState = $Journal.PriorFiles[$Name]
        if ($PriorState.BackupPrepared -and $PriorState.Removal.Attempted -and
            -not $PriorState.Removal.Completed -and
            -not (Test-Path -LiteralPath $LivePath)) {
            $PriorState.Removal.Completed = $true
        }

        $PublishedState = $Journal.PublishedFiles[$Name]
        $StagePath = Join-Path $TransactionRoot $Name
        if ($PublishedState.Attempted -and -not $PublishedState.Completed -and
            -not (Test-Path -LiteralPath $StagePath) -and
            (Test-Path -LiteralPath $LivePath)) {
            $PublishedState.Completed = $true
        }
    }
    foreach ($StateDirectoryJournal in $Journal.StateDirectories.Values) {
        if ($StateDirectoryJournal.Attempted -and
            -not $StateDirectoryJournal.Completed -and
            (Test-Path -LiteralPath $StateDirectoryJournal.Path)) {
            $StateDirectoryJournal.Completed = $true
        }
    }
}

function Test-RollbackState {
    foreach ($Name in $ManagedNames) {
        $LivePath = Join-Path $InstallRoot $Name
        $PriorState = $Journal.PriorFiles[$Name]
        if ($PriorState.Existed -and -not (Test-Path -LiteralPath $LivePath)) {
            return $false
        }
        if (-not $PriorState.Existed -and (Test-Path -LiteralPath $LivePath)) {
            return $false
        }
        if ($PriorState.Existed -and
            -not (Test-FileSystemSecurityTree $PriorState.SecuritySnapshots)) {
            return $false
        }
    }
    foreach ($StateDirectoryJournal in $Journal.StateDirectories.Values) {
        if ($StateDirectoryJournal.Completed -and
            (Test-Path -LiteralPath $StateDirectoryJournal.Path)) {
            return $false
        }
    }
    if (-not $InstallRootWasPresent -and $Journal.InstallRoot.Attempted -and
        (Test-Path -LiteralPath $InstallRoot)) {
        return $false
    }
    if ($InstallRootWasPresent -and
        $Journal.InstallRoot.Restriction.Attempted -and
        -not (Test-FileSystemSecuritySnapshot
            $Journal.InstallRoot.SecuritySnapshot)) {
        return $false
    }

    $ObservedTask = Get-RegisteredTask -Folder $TaskFolder -Name $TaskName
    if ($PriorTaskWasPresent) {
        if ($null -eq $ObservedTask) { return $false }
        $ObservedTaskSddl =
            $ObservedTask.GetSecurityDescriptor($TaskSecurityInformation)
        if ($ObservedTaskSddl -ne $PriorTaskSddl) { return $false }
        $ObservedRunning = [int]$ObservedTask.State -eq $TaskStateRunning
        if ($ObservedRunning -ne $PriorTaskWasActive) { return $false }
    }
    elseif ($null -ne $ObservedTask) {
        return $false
    }
    return $true
}

function Invoke-Rollback {
    $RollbackFailed = $false

    if ($Journal.TaskRegistration.Attempted) {
        try {
            $CurrentTask = Get-RegisteredTask -Folder $TaskFolder -Name $TaskName
            if ($null -ne $CurrentTask) {
                if ([int]$CurrentTask.State -eq $TaskStateRunning -or
                    [int]$CurrentTask.State -eq $TaskStateQueued) {
                    $CurrentTask.Stop(0)
                }
                $TaskFolder.DeleteTask($TaskName, 0)
            }
        }
        catch { $RollbackFailed = $true }
    }

    foreach ($Name in $ManagedNames) {
        $LivePath = Join-Path $InstallRoot $Name
        $PriorState = $Journal.PriorFiles[$Name]
        $PublishedState = $Journal.PublishedFiles[$Name]
        if ($PublishedState.Completed) {
            try {
                Remove-SafePath $LivePath
            }
            catch { $RollbackFailed = $true }
        }
        if ($PriorState.BackupPrepared -and $PriorState.Removal.Attempted) {
            try {
                $BackupPath = Join-Path $BackupRoot $Name
                Restore-BackupPath -Source $BackupPath -Destination $LivePath
            }
            catch { $RollbackFailed = $true }
        }
        if ($PriorState.Existed -and (Test-Path -LiteralPath $LivePath)) {
            try {
                Restore-FileSystemSecurityTree $PriorState.SecuritySnapshots
            }
            catch { $RollbackFailed = $true }
        }
    }

    foreach ($StateDirectoryJournal in $Journal.StateDirectories.Values) {
        if ($StateDirectoryJournal.Completed) {
            try {
                Remove-SafePath $StateDirectoryJournal.Path
            }
            catch { $RollbackFailed = $true }
        }
    }

    if (-not $InstallRootWasPresent -and $Journal.InstallRoot.Attempted) {
        try {
            if (Test-Path -LiteralPath $InstallRoot) {
                $RemainingItems = @(Get-ChildItem -LiteralPath $InstallRoot -Force)
                if ($RemainingItems.Count -eq 0) {
                    Remove-SafePath $InstallRoot
                }
            }
        }
        catch { $RollbackFailed = $true }
    }
    elseif ($InstallRootWasPresent -and
        $Journal.InstallRoot.Restriction.Attempted) {
        try {
            Restore-FileSystemSecuritySnapshot `
                $Journal.InstallRoot.SecuritySnapshot
        }
        catch { $RollbackFailed = $true }
    }

    if ($PriorTaskWasPresent) {
        if ($Journal.TaskRegistration.Attempted) {
            try {
                $RestoredTaskXml = [System.IO.File]::ReadAllText($PriorTaskXmlPath)
                [void]$TaskFolder.RegisterTask(
                    $TaskName,
                    $RestoredTaskXml,
                    $TaskRestoreFlags,
                    "SYSTEM",
                    $null,
                    $TaskLogonServiceAccount,
                    $PriorTaskSddl
                )
            }
            catch { $RollbackFailed = $true }
        }
        if ($PriorTaskWasActive -and
            ($Journal.PriorTaskStop.Attempted -or
                $Journal.TaskRegistration.Attempted)) {
            try {
                $RestoredTask = Get-RegisteredTask -Folder $TaskFolder -Name $TaskName
                if ($null -eq $RestoredTask) { throw "task restoration failed" }
                if ([int]$RestoredTask.State -ne $TaskStateRunning) {
                    [void]$RestoredTask.Run($null)
                    if (-not (Wait-TaskRunning -Folder $TaskFolder -Name $TaskName)) {
                        throw "task restart failed"
                    }
                }
            }
            catch { $RollbackFailed = $true }
        }
    }

    try {
        if (-not (Test-RollbackState)) { $RollbackFailed = $true }
    }
    catch { $RollbackFailed = $true }

    if (-not $RollbackFailed) {
        try {
            Remove-SafePath $TransactionRoot
            Remove-SafePath $BackupRoot
            Remove-SafePath $RecoveryRoot
        }
        catch { $RollbackFailed = $true }
    }
    return -not $RollbackFailed
}

$Journal = @{
    InstallRoot = @{
        Attempted = $false
        Completed = $false
        SecuritySnapshot = $null
        Restriction = @{ Attempted = $false; Completed = $false }
    }
    PriorTaskStop = @{ Attempted = $false; Completed = $false }
    TaskRegistration = @{ Attempted = $false; Completed = $false }
    TaskStart = @{ Attempted = $false; Completed = $false }
    PriorFiles = @{}
    PublishedFiles = @{}
    StateDirectories = @{}
}
foreach ($Name in $ManagedNames) {
    $Journal.PriorFiles[$Name] = @{
        Existed = $false
        BackupPrepared = $false
        SecuritySnapshots = @()
        Removal = @{ Attempted = $false; Completed = $false }
    }
    $Journal.PublishedFiles[$Name] = @{ Attempted = $false; Completed = $false }
}

try {
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
    if (-not (Test-PathCommand "py") -or
        -not (Test-PathCommand "schtasks.exe") -or
        -not (Test-PathCommand "icacls.exe")) {
        Fail "required Windows command is unavailable"
    }
    Assert-SafeDirectory $InstallParent "install parent"
    Assert-SafeDirectory $InstallRoot "install root"
    $InstallRootWasPresent = Test-Path -LiteralPath $InstallRoot
    if ($InstallRootWasPresent) {
        $Journal.InstallRoot.SecuritySnapshot =
            Get-FileSystemSecuritySnapshot $InstallRoot
    }
    if (Test-Path -LiteralPath $RecoveryRoot) {
        Fail "recovery-required at C:\ProgramData\.monitor-agent-recovery"
    }
    foreach ($Name in $ManagedNames) {
        $LivePath = Join-Path $InstallRoot $Name
        Assert-SafeManagedPath $LivePath $Name
        $Journal.PriorFiles[$Name].Existed = Test-Path -LiteralPath $LivePath
        if ($Journal.PriorFiles[$Name].Existed) {
            $PriorState = $Journal.PriorFiles[$Name]
            $PriorState.SecuritySnapshots = Get-FileSystemSecurityTree $LivePath
        }
    }
    $TaskFolder = Connect-TaskScheduler

    $FailureCategory = "staging"
    New-Item -ItemType Directory -LiteralPath $RecoveryRoot -Force | Out-Null
    Set-RestrictedAcl $RecoveryRoot
    New-Item -ItemType Directory -LiteralPath $TransactionRoot -Force | Out-Null
    Set-RestrictedAcl $TransactionRoot
    New-Item -ItemType Directory -LiteralPath $BackupRoot -Force | Out-Null
    Set-RestrictedAcl $BackupRoot

    $StageVenv = Join-Path $TransactionRoot "venv"
    $StageConfig = Join-Path $TransactionRoot "monitor-agent.env"
    $StageLauncher = Join-Path $TransactionRoot "run-agent.ps1"
    $StageTask = Join-Path $TransactionRoot "monitor_agent_task.xml"
    $StageLock = Join-Path $TransactionRoot "requirements.lock"
    $StageWheel = Join-Path $TransactionRoot (Split-Path -Leaf $WheelPath)
    $StagePython = Join-Path $StageVenv "Scripts\python.exe"
    Copy-Item -LiteralPath $EnvironmentFile -Destination $StageConfig -Force
    Copy-Item -LiteralPath $LauncherSource -Destination $StageLauncher -Force
    Copy-Item -LiteralPath $TaskSource -Destination $StageTask -Force
    Copy-Item -LiteralPath $LockPath -Destination $StageLock -Force
    Copy-Item -LiteralPath $WheelPath -Destination $StageWheel -Force
    Set-RestrictedAcl $TransactionRoot -Recurse

    py "-$PythonVersion" -m venv $StageVenv
    if ($LASTEXITCODE -ne 0) { Fail "virtual environment creation failed" }
    Assert-RegularFile $StagePython "staged Python executable"
    & $StagePython -m pip install --require-hashes -r $StageLock
    if ($LASTEXITCODE -ne 0) { Fail "locked dependency installation failed" }
    & $StagePython -m pip install --no-deps --force-reinstall $StageWheel
    if ($LASTEXITCODE -ne 0) { Fail "wheel installation failed" }
    & $StageLauncher -Command check-config -InstallRoot $TransactionRoot
    if ($LASTEXITCODE -ne 0) { Fail "staged configuration validation failed" }

    $FailureCategory = "prior-task-capture"
    $PriorTask = Get-RegisteredTask -Folder $TaskFolder -Name $TaskName
    $PriorTaskWasPresent = $null -ne $PriorTask
    if ($PriorTaskWasPresent) {
        $PriorTaskXml = $PriorTask.Xml
        $PriorTaskSddl = $PriorTask.GetSecurityDescriptor($TaskSecurityInformation)
        $PriorTaskState = [int]$PriorTask.State
        $PriorTaskWasRunning = $PriorTaskState -eq $TaskStateRunning
        $PriorTaskWasActive = (
            $PriorTaskState -eq $TaskStateRunning -or
            $PriorTaskState -eq $TaskStateQueued
        )
        $Utf8WithoutBom = New-Object System.Text.UTF8Encoding($false)
        [System.IO.File]::WriteAllText($PriorTaskXmlPath, $PriorTaskXml, $Utf8WithoutBom)
        Set-RestrictedAcl $PriorTaskXmlPath
    }

    $MutationStarted = $true
    $FailureCategory = "install-root-security"
    if (-not (Test-Path -LiteralPath $InstallRoot)) {
        [void](Invoke-JournaledMutation -Name "install-root" `
            -State $Journal.InstallRoot -Action {
                New-Item -ItemType Directory -LiteralPath $InstallRoot -Force |
                    Out-Null
            })
    }
    Assert-SafeDirectory $InstallRoot "install root"
    [void](Invoke-JournaledMutation -Name "restrict-install-root" `
        -State $Journal.InstallRoot.Restriction -Action {
            Set-RestrictedAcl $InstallRoot
        })

    if ($PriorTaskWasPresent -and $PriorTaskWasActive) {
        $FailureCategory = "prior-task-stop"
        [void](Invoke-JournaledMutation -Name "prior-task-stop" `
            -State $Journal.PriorTaskStop -Action {
                $PriorTask.Stop(0)
            })
        $StoppedTask = Get-RegisteredTask -Folder $TaskFolder -Name $TaskName
        if ($null -eq $StoppedTask -or
            [int]$StoppedTask.State -eq $TaskStateRunning -or
            [int]$StoppedTask.State -eq $TaskStateQueued) {
            Fail "prior task stop verification failed"
        }
    }

    $FailureCategory = "live-files"
    foreach ($Name in $ManagedNames) {
        $LivePath = Join-Path $InstallRoot $Name
        $PriorState = $Journal.PriorFiles[$Name]
        if ($PriorState.Existed) {
            $BackupPath = Join-Path $BackupRoot $Name
            Copy-Item -LiteralPath $LivePath -Destination $BackupPath -Recurse -Force
            Set-RestrictedAcl $BackupPath -Recurse
            $PriorState.BackupPrepared = $true
            [void](Invoke-JournaledMutation -Name "remove-prior-file" `
                -State $PriorState.Removal -Action {
                    Remove-SafePath $LivePath
                })
        }
    }

    foreach ($Name in $ManagedNames) {
        $StagePath = Join-Path $TransactionRoot $Name
        $LivePath = Join-Path $InstallRoot $Name
        $PublishedState = $Journal.PublishedFiles[$Name]
        [void](Invoke-JournaledMutation -Name "publish-file" `
            -State $PublishedState -Action {
                Move-Item -LiteralPath $StagePath -Destination $LivePath
            })
        Set-RestrictedAcl $LivePath -Recurse
    }

    foreach ($StateDirectory in @("logs", "spool")) {
        $StatePath = Join-Path $InstallRoot $StateDirectory
        Assert-SafeDirectory $StatePath $StateDirectory
        if (-not (Test-Path -LiteralPath $StatePath)) {
            $StateDirectoryJournal = @{
                Attempted = $false
                Completed = $false
                Path = $StatePath
            }
            $Journal.StateDirectories[$StateDirectory] = $StateDirectoryJournal
            [void](Invoke-JournaledMutation -Name "create-state-directory" `
                -State $StateDirectoryJournal -Action {
                    New-Item -ItemType Directory -LiteralPath $StatePath -Force |
                        Out-Null
                })
        }
    }

    $FailureCategory = "replacement-registration"
    $ReplacementTaskXml = [System.IO.File]::ReadAllText(
        (Join-Path $InstallRoot "monitor_agent_task.xml")
    )
    $ReplacementTask = Invoke-JournaledMutation -Name "register-task" `
        -State $Journal.TaskRegistration -Action {
            $TaskFolder.RegisterTask(
                $TaskName,
                $ReplacementTaskXml,
                $TaskCreateOrUpdate,
                "SYSTEM",
                $null,
                $TaskLogonServiceAccount,
                $null
            )
        }
    if ($null -eq $ReplacementTask) { Fail "task registration failed" }

    $FailureCategory = "replacement-start"
    $RunningTask = Invoke-JournaledMutation -Name "start-task" `
        -State $Journal.TaskStart -Action {
            $ReplacementTask.Run($null)
        }
    if ($null -eq $RunningTask) { Fail "task start failed" }

    $FailureCategory = "replacement-readiness"
    if (-not (Wait-TaskRunning -Folder $TaskFolder -Name $TaskName)) {
        Fail "replacement task readiness failed"
    }

    $DeploymentCommitted = $true
    $FailureCategory = "cleanup"
    Remove-SafePath $TransactionRoot
    Remove-SafePath $BackupRoot
    Remove-SafePath $RecoveryRoot
    $Succeeded = $true
}
catch {
    if ($DeploymentCommitted) {
        Write-Error ("monitor-agent install: deployment committed; " +
            "recovery cleanup required at C:\ProgramData\.monitor-agent-recovery")
        exit 1
    }

    $RollbackComplete = $false
    if ($MutationStarted) {
        Resolve-AmbiguousJournal
        $RollbackComplete = Invoke-Rollback
    }
    else {
        try {
            Remove-SafePath $RecoveryRoot
            $RollbackComplete = -not (Test-Path -LiteralPath $RecoveryRoot)
        }
        catch { $RollbackComplete = $false }
    }

    if ($RollbackComplete) {
        Write-Error "monitor-agent install: deployment failed ($FailureCategory); rollback complete"
    }
    else {
        Write-Error ("monitor-agent install: deployment failed ($FailureCategory); " +
            "recovery-required at C:\ProgramData\.monitor-agent-recovery")
    }
    exit 1
}

if ($Succeeded) { Write-Host "monitor-agent install: task deployed" }
