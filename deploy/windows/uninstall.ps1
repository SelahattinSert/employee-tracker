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
    param([Parameter(Mandatory = $true)][string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) { return $false }
    $Item = Get-Item -LiteralPath $Path -Force
    if (Test-ReparsePoint $Item) { Fail "managed target is unsafe" }
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

function Verify-TaskAbsent {
    param([Parameter(Mandatory = $true)]$Folder)
    if ($null -ne (Get-RegisteredTask -Folder $Folder -Name $TaskName)) {
        Fail "task is still registered"
    }
}

function Remove-SafePath {
    param([Parameter(Mandatory = $true)][string]$Path)
    if (Assert-SafeManagedPath $Path) {
        Remove-Item -LiteralPath $Path -Recurse -Force
    }
}

function Remove-RuntimeArtifacts {
    foreach ($Name in @("venv", "run-agent.ps1", "monitor_agent_task.xml")) {
        Remove-SafePath (Join-Path $InstallRoot $Name)
    }
}

try {
    $Principal = New-Object Security.Principal.WindowsPrincipal(
        [Security.Principal.WindowsIdentity]::GetCurrent()
    )
    if (-not $Principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        Fail "Administrator privileges required"
    }

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
    $RootItem = Get-Item -LiteralPath $InstallRoot -Force
    if (-not $RootItem.PSIsContainer -or (Test-ReparsePoint $RootItem)) {
        Fail "install root is unsafe"
    }
    if ($Purge) {
        Remove-Item -LiteralPath $InstallRoot -Recurse -Force
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
