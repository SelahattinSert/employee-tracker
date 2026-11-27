<#
Configuration step for the Monitor Agent GUI installer (installer.iss runs this after it
copies the payload into Program Files). It creates the virtual environment, installs the
bundled wheel, writes an owner-only configuration from the wizard's answers and registers
the scheduled tasks. No secret is embedded here; the token arrives through -TokenFile and
the file is the caller's to delete.

This does NOT weaken or evade Windows Defender. It installs under Program Files and runs as
the installer's elevated process; if Defender flags the unsigned build, sign it or have an
administrator add an explicit exclusion deliberately.
#>
param(
    [Parameter(Mandatory = $true)][string]$InstallDir,
    [Parameter(Mandatory = $true)][string]$ServerUrl,
    [Parameter(Mandatory = $true)][string]$TokenFile,
    [string]$CaFile,
    [switch]$EmployeesNotified,
    [string]$AuditPaths = "C:\Users"
)

$ErrorActionPreference = "Stop"
$LogPath = Join-Path $InstallDir "install-log.txt"
$SupportedPythonVersions = @("3.14", "3.13", "3.12", "3.11")

function Write-Log {
    param([string]$Message)
    $line = "{0} {1}" -f (Get-Date -Format "s"), $Message
    Add-Content -LiteralPath $LogPath -Value $line
    Write-Host $line
}

function Fail {
    param([string]$Message)
    Write-Log "ERROR: $Message"
    exit 1
}

function ConvertTo-CollectorUri {
    param([string]$Value)
    $text = $Value.Trim()
    if ($text -notmatch "^[A-Za-z][A-Za-z0-9+.-]*://") { $text = "https://$text" }
    try { $uri = [Uri]$text } catch { Fail "The server address is not a valid URL." }
    if ($uri.Scheme -ne "https") { Fail "The server address must use HTTPS." }
    if (-not [string]::IsNullOrEmpty($uri.UserInfo)) {
        Fail "The server address must not contain credentials."
    }
    if ($uri.AbsolutePath -eq "/") {
        return $uri.GetLeftPart([UriPartial]::Authority) + "/api/v1/telemetry"
    }
    return $uri.GetLeftPart([UriPartial]::Path)
}

function Find-PythonVersion {
    $ErrorActionPreference = "Continue"
    if (-not (Get-Command py -ErrorAction SilentlyContinue)) { return $null }
    foreach ($version in $SupportedPythonVersions) {
        & py "-$version" -c "import sys" 2>$null | Out-Null
        if ($LASTEXITCODE -eq 0) { return $version }
    }
    return $null
}

function Install-PythonWithWinget {
    $ErrorActionPreference = "Continue"
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) { return }
    Write-Log "Installing Python 3.14 for all users with winget."
    winget install --exact --id Python.Python.3.14 --scope machine `
        --accept-package-agreements --accept-source-agreements
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
        [Environment]::GetEnvironmentVariable("Path", "User")
}

function Restrict-Acl {
    param([string]$Path)
    & icacls.exe $Path /inheritancelevel:r | Out-Null
    & icacls.exe $Path /grant:r "*S-1-5-18:(F)" "*S-1-5-32-544:(F)" | Out-Null
}

try {
    New-Item -ItemType Directory -Path $InstallDir -Force | Out-Null
    Set-Content -LiteralPath $LogPath -Value "" -Encoding utf8
    Write-Log "Monitor Agent configuration started in $InstallDir"

    if (-not (Test-Path -LiteralPath $TokenFile)) { Fail "Token file is missing." }
    $token = (Get-Content -LiteralPath $TokenFile -Raw).Trim()
    if ([string]::IsNullOrWhiteSpace($token) -or $token -match "\s") {
        Fail "The API token is empty or contains whitespace."
    }
    $collectorUri = ConvertTo-CollectorUri $ServerUrl
    Write-Log "Collector URL: $collectorUri"

    $wheel = Get-ChildItem -LiteralPath $InstallDir -Filter "monitor_agent-*.whl" |
        Select-Object -First 1
    if ($null -eq $wheel) { Fail "The bundled wheel is missing from the install folder." }
    $lock = Join-Path $InstallDir "requirements.lock"
    if (-not (Test-Path -LiteralPath $lock)) { Fail "requirements.lock is missing." }

    $python = Find-PythonVersion
    if ($null -eq $python) {
        Install-PythonWithWinget
        $python = Find-PythonVersion
    }
    if ($null -eq $python) {
        Fail "Python 3.11-3.14 was not found. Install it from https://www.python.org/downloads/."
    }
    Write-Log "Using Python $python"

    $venv = Join-Path $InstallDir "venv"
    $venvPython = Join-Path $venv "Scripts\python.exe"
    if (-not (Test-Path -LiteralPath $venvPython)) {
        & py "-$python" -m venv $venv
        if ($LASTEXITCODE -ne 0) { Fail "Virtual environment creation failed." }
    }
    & $venvPython -m pip install --require-hashes -r $lock
    if ($LASTEXITCODE -ne 0) { Fail "Locked dependency installation failed." }
    & $venvPython -m pip install --no-deps --force-reinstall $wheel.FullName
    if ($LASTEXITCODE -ne 0) { Fail "Agent installation failed." }

    $caBundle = $null
    if ($CaFile -and (Test-Path -LiteralPath $CaFile)) {
        $caTarget = "C:\ProgramData\MonitorAgentCA"
        New-Item -ItemType Directory -Path $caTarget -Force | Out-Null
        Copy-Item -LiteralPath $CaFile -Destination (Join-Path $caTarget "ca.crt") -Force
        $caBundle = Join-Path $caTarget "ca.crt"
        Write-Log "Private CA installed at $caBundle"
    }

    $notified = [bool]$EmployeesNotified
    $lines = [System.Collections.Generic.List[string]]::new()
    $lines.Add("MONITOR_COLLECTOR_URI=$collectorUri")
    $lines.Add("MONITOR_API_TOKEN=$token")
    if ($caBundle) { $lines.Add("MONITOR_CA_BUNDLE=$caBundle") }
    $lines.Add("MONITOR_HEARTBEAT_SEC=300")
    $lines.Add("MONITOR_STARTUP_DELAY_SEC=30")
    $lines.Add("MONITOR_SPOOL_PATH=C:\ProgramData\MonitorAgent\spool")
    $lines.Add("MONITOR_LOG_PATH=C:\ProgramData\MonitorAgent\logs\monitor-agent.log")
    $lines.Add("MONITOR_LOG_FORMAT=json")
    $lines.Add("MONITOR_PROCESS_CMDLINE_MODE=redacted")
    $lines.Add("MONITOR_SESSION_SOURCE=helper")
    $lines.Add("MONITOR_SCREENSHOT_ENABLED=$($notified.ToString().ToLower())")
    $lines.Add("MONITOR_INCLUDE_ACTIVE_WINDOW=$($notified.ToString().ToLower())")
    $lines.Add("MONITOR_EMPLOYEE_NOTICE_ACK=$($notified.ToString().ToLower())")
    $lines.Add("MONITOR_AUDIT_PATHS=$(if ($notified) { $AuditPaths } else { '' })")

    $configPath = Join-Path $InstallDir "monitor-agent.env"
    $encoding = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllLines($configPath, $lines, $encoding)
    Restrict-Acl $configPath
    Write-Log "Configuration written (sensitive collectors $(if ($notified) {'enabled'} else {'disabled'}))."

    $agentCommand = "`"$venvPython`" -m monitor_agent run"
    & schtasks.exe /Create /TN "MonitorAgent" /SC ONSTART /RU "SYSTEM" /RL HIGHEST `
        /TR $agentCommand /F
    if ($LASTEXITCODE -ne 0) { Fail "Could not register the MonitorAgent scheduled task." }

    $helperPythonw = Join-Path $venv "Scripts\pythonw.exe"
    if (Test-Path -LiteralPath $helperPythonw) {
        $helperCommand = "`"$helperPythonw`" -m monitor_agent session-helper"
        & schtasks.exe /Create /TN "MonitorAgentSession" /SC ONLOGON /RL LIMITED `
            /TR $helperCommand /F | Out-Null
    }

    & schtasks.exe /Run /TN "MonitorAgent" | Out-Null
    Write-Log "Monitor Agent installed and started. It appears on the server after its first event."
    exit 0
}
catch {
    Fail $_.Exception.Message
}
