param([switch]$Purge)

$ErrorActionPreference = "Stop"
$TaskName = "MonitorAgent"
$InstallRoot = "C:\ProgramData\MonitorAgent"
$TaskNotFoundHResult = -2147024894
$TaskStateQueued = 2
$TaskStateRunning = 4

function Fail {
    param([Parameter(Mandatory = $true)][string]$Message)
    throw "monitor-agent uninstall: $Message"
}

function Test-ReparsePoint {
    param([Parameter(Mandatory = $true)][System.IO.FileSystemInfo]$Item)
    return (($Item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0)
}

function Assert-SafeManagedPath {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][bool]$ExpectedDirectory
    )
    if (-not (Test-Path -LiteralPath $Path)) { return $false }
    $Item = Get-Item -LiteralPath $Path -Force
    if (Test-ReparsePoint $Item) { Fail "managed target is unsafe" }
    if ($Item.PSIsContainer -ne $ExpectedDirectory) {
        Fail "managed target type is unsafe"
    }
    return $true
}

function Get-SafeTreeItems {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [string]$Label = "tree"
    )
    if (-not (Test-Path -LiteralPath $Path)) { return @() }

    $RootItem = Get-Item -LiteralPath $Path -Force
    if (Test-ReparsePoint $RootItem) { Fail "$Label contains a reparse point" }

    $Items = New-Object System.Collections.ArrayList
    [void]$Items.Add($RootItem)
    $Pending = New-Object System.Collections.Stack
    if ($RootItem.PSIsContainer) {
        $Pending.Push($RootItem.FullName)
    }
    while ($Pending.Count -gt 0) {
        $CurrentPath = [string]$Pending.Pop()
        foreach ($Child in @(
            Get-ChildItem -LiteralPath $CurrentPath -Force
        )) {
            if (Test-ReparsePoint $Child) {
                Fail "$Label contains a reparse point"
            }
            [void]$Items.Add($Child)
            if ($Child.PSIsContainer) {
                $Pending.Push($Child.FullName)
            }
        }
    }
    return $Items.ToArray()
}

function Assert-SafeTree {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [string]$Label = "tree"
    )
    $null = @(Get-SafeTreeItems -Path $Path -Label $Label)
}

function Assert-UninstallTargetsSafe {
    if (-not (Test-Path -LiteralPath $InstallRoot)) { return }
    [void](Assert-SafeManagedPath -Path $InstallRoot -ExpectedDirectory $true)
    Assert-SafeTree $InstallRoot "install tree"
    $VenvPath = Join-Path $InstallRoot "venv"
    if (Assert-SafeManagedPath -Path $VenvPath -ExpectedDirectory $true) {
        Assert-SafeTree $VenvPath "venv tree"
    }
    $RuntimeTargets = @(
        @{ Name = "run-agent.ps1"; ExpectedDirectory = $false },
        @{ Name = "monitor_agent_task.xml"; ExpectedDirectory = $false }
    )
    foreach ($RuntimeTarget in $RuntimeTargets) {
        [void](Assert-SafeManagedPath `
            -Path (Join-Path $InstallRoot $RuntimeTarget.Name) `
            -ExpectedDirectory $RuntimeTarget.ExpectedDirectory)
    }
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

function Verify-TaskAbsent {
    param([Parameter(Mandatory = $true)]$Folder)
    if ($null -ne (Get-RegisteredTask -Folder $Folder -Name $TaskName)) {
        Fail "task is still registered"
    }
}

function Remove-SafePath {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][bool]$ExpectedDirectory
    )
    if (Assert-SafeManagedPath `
        -Path $Path `
        -ExpectedDirectory $ExpectedDirectory) {
        Assert-SafeTree -Path $Path -Label "removal tree"
        Remove-Item -LiteralPath $Path -Recurse -Force
    }
}

function Remove-RuntimeArtifacts {
    Remove-SafePath `
        -Path (Join-Path $InstallRoot "venv") `
        -ExpectedDirectory $true
    Remove-SafePath `
        -Path (Join-Path $InstallRoot "run-agent.ps1") `
        -ExpectedDirectory $false
    Remove-SafePath `
        -Path (Join-Path $InstallRoot "monitor_agent_task.xml") `
        -ExpectedDirectory $false
}

try {
    $Principal = New-Object Security.Principal.WindowsPrincipal(
        [Security.Principal.WindowsIdentity]::GetCurrent()
    )
    if (-not $Principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        Fail "Administrator privileges required"
    }

    Assert-UninstallTargetsSafe
    $TaskFolder = Connect-TaskScheduler
    $RegisteredTask = Get-RegisteredTask -Folder $TaskFolder -Name $TaskName
    if ($null -ne $RegisteredTask) {
        if ([int]$RegisteredTask.State -eq $TaskStateRunning -or
            [int]$RegisteredTask.State -eq $TaskStateQueued) {
            $RegisteredTask.Stop(0)
            $StoppedTask = Get-RegisteredTask -Folder $TaskFolder -Name $TaskName
            if ($null -eq $StoppedTask -or
                [int]$StoppedTask.State -eq $TaskStateRunning -or
                [int]$StoppedTask.State -eq $TaskStateQueued) {
                Fail "unable to stop task"
            }
        }
        $TaskFolder.DeleteTask($TaskName, 0)
    }
    Verify-TaskAbsent -Folder $TaskFolder

    if (-not (Test-Path -LiteralPath $InstallRoot)) {
        Write-Host "monitor-agent uninstall: task absent; no runtime files found"
        exit 0
    }
    Assert-UninstallTargetsSafe
    if ($Purge) {
        Remove-SafePath -Path $InstallRoot -ExpectedDirectory $true
        Write-Host "monitor-agent uninstall: installation purged"
        exit 0
    }

    Remove-RuntimeArtifacts
    Write-Host "monitor-agent uninstall: preserved monitor-agent.env, logs, and spool"
}
catch {
    Write-Error "monitor-agent uninstall: failed"
    exit 1
}
