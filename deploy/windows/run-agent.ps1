param(
    [ValidateSet("run", "check-config", "health")]
    [string]$Command = "run",
    [string]$InstallRoot = "C:\ProgramData\MonitorAgent"
)

$ErrorActionPreference = "Stop"
$ConfigPath = Join-Path $InstallRoot "monitor-agent.env"
$AgentPath = Join-Path $InstallRoot "venv\Scripts\monitor-agent.exe"
$KnownEnvironmentKeys = @(
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
    "MONITOR_LOG_LEVEL"
)

function Test-ReparsePoint {
    param([Parameter(Mandatory = $true)][System.IO.FileSystemInfo]$Item)
    return (($Item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0)
}

function Test-RegularFile {
    param([Parameter(Mandatory = $true)][string]$Path)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        return $false
    }
    $Item = Get-Item -LiteralPath $Path -Force
    return -not (Test-ReparsePoint $Item)
}

function Clear-KnownEnvironment {
    foreach ($Name in $KnownEnvironmentKeys) {
        [Environment]::SetEnvironmentVariable($Name, $null, "Process")
    }
}

if (-not (Test-RegularFile $ConfigPath)) {
    throw "Monitor Agent configuration is missing"
}
if (-not (Test-RegularFile $AgentPath)) {
    throw "Monitor Agent executable is missing"
}

Clear-KnownEnvironment
$SeenEnvironmentKeys = New-Object -TypeName "System.Collections.Generic.HashSet[string]"
foreach ($Line in Get-Content -LiteralPath $ConfigPath -Encoding UTF8) {
    if ($Line.IndexOf([char]0) -ge 0) {
        throw "Invalid Monitor Agent environment entry"
    }
    $Trimmed = $Line.Trim()
    if ($Trimmed.Length -eq 0 -or $Trimmed.StartsWith("#")) {
        continue
    }
    $TrimmedParts = $Trimmed.Split("=", 2)
    $Parts = $Line.Split("=", 2)
    if ($TrimmedParts.Count -ne 2 -or $Parts.Count -ne 2 -or $Parts[0] -ne $Parts[0].Trim() -or
        $Parts[0] -notmatch "^[A-Z][A-Z0-9_]+$" -or
        $KnownEnvironmentKeys -notcontains $Parts[0] -or
        $SeenEnvironmentKeys.Contains($Parts[0])) {
        throw "Invalid Monitor Agent environment entry"
    }
    [void]$SeenEnvironmentKeys.Add($Parts[0])
    [Environment]::SetEnvironmentVariable($Parts[0], $Parts[1], "Process")
}

& $AgentPath $Command
exit $LASTEXITCODE
