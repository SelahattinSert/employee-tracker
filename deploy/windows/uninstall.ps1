param([switch]$Purge)

$ErrorActionPreference = "Stop"
$TaskName = "MonitorAgent"
$InstallRoot = "C:\ProgramData\MonitorAgent"

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

function Test-TaskExists {
    $TaskOutput = (& schtasks.exe /Query /TN MonitorAgent 2>&1 | Out-String)
    if ($LASTEXITCODE -eq 0) { return $true }
    if ($LASTEXITCODE -eq 1 -and $TaskOutput -match "(?i)(cannot find|does not exist)") {
        return $false
    }
    Fail "unable to query task"
}

function Verify-TaskAbsent {
    if (Test-TaskExists) { Fail "task is still registered" }
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
    if ($null -eq (Get-Command schtasks.exe -CommandType Application -ErrorAction Ignore)) {
        Fail "schtasks.exe is unavailable"
    }

    if (Test-TaskExists) {
        $TaskDetails = (& schtasks.exe /Query /TN MonitorAgent /FO LIST /V 2>&1 | Out-String)
        if ($LASTEXITCODE -ne 0) { Fail "unable to inspect task" }
        if ($TaskDetails -match "Running") {
            & schtasks.exe /End /TN MonitorAgent | Out-Null
            if ($LASTEXITCODE -ne 0) { Fail "unable to stop task" }
        }
        & schtasks.exe /Delete /TN MonitorAgent /F | Out-Null
        if ($LASTEXITCODE -ne 0) { Fail "unable to delete task" }
    }
    Verify-TaskAbsent

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
