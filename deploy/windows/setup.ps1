<#
Interactive Monitor Agent setup for Windows. Install.cmd runs it elevated.

Nothing secret ships with the package: the collector URL and API token are asked for at
install time (or read from -TokenFile), written to a temporary owner-only configuration,
handed to install.ps1 and deleted again. A private CA certificate is optional; when a
file named ca.crt sits next to Install.cmd (or -CaFile is given) it is installed under
C:\ProgramData\MonitorAgentCA and referenced through MONITOR_CA_BUNDLE.

Screenshots, active-window titles and file audit are sensitive telemetry. Setup asks once
whether employees have been notified (default yes); only then are those collectors and the
MONITOR_EMPLOYEE_NOTICE_ACK compliance flag written as enabled. For unattended installs
pass -EmployeesNotified or -NoEmployeesNotified. The agent code keeps these collectors off
by default, so a plain "monitor-agent" run never collects them unless this file enables it.
#>
param(
    [string]$ServerUrl,
    [string]$TokenFile,
    [string]$CaFile,
    [ValidateSet("3.11", "3.12", "3.13", "3.14")]
    [string]$PythonVersion,
    [switch]$EmployeesNotified,
    [switch]$NoEmployeesNotified,
    [string]$AuditPaths
)

$ErrorActionPreference = "Stop"
$ScriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$PackageRoot = Split-Path -Parent (Split-Path -Parent $ScriptRoot)
$Installer = Join-Path $ScriptRoot "install.ps1"
$EnvironmentExample = Join-Path $ScriptRoot "monitor-agent.env.example"
$CaTargetDirectory = "C:\ProgramData\MonitorAgentCA"
$CaTargetPath = Join-Path $CaTargetDirectory "ca.crt"
$DefaultIngestPath = "/api/v1/telemetry"
# Watched by default when sensitive collection is enabled; bounded by MONITOR_AUDIT_MAX_FILES.
$DefaultAuditPaths = "C:\Users"
$SupportedPythonVersions = @("3.14", "3.13", "3.12", "3.11")

function Fail {
    param([Parameter(Mandatory = $true)][string]$Message)
    throw $Message
}

function Assert-Administrator {
    $Principal = New-Object Security.Principal.WindowsPrincipal(
        [Security.Principal.WindowsIdentity]::GetCurrent()
    )
    if (-not $Principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        Fail "Run Install.cmd (or this script) as Administrator."
    }
}

function Find-PythonVersion {
    param([string]$Requested)
    # Windows PowerShell 5.1 turns redirected native stderr into terminating errors under Stop.
    $ErrorActionPreference = "Continue"
    if (-not (Get-Command py -ErrorAction SilentlyContinue)) { return $null }
    $Candidates = $SupportedPythonVersions
    if ($Requested) { $Candidates = @($Requested) }
    foreach ($Version in $Candidates) {
        & py "-$Version" -c "import sys" 2>$null | Out-Null
        if ($LASTEXITCODE -eq 0) { return $Version }
    }
    return $null
}

function Install-PythonWithWinget {
    $ErrorActionPreference = "Continue"
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) { return }
    Write-Host "Python 3.11-3.14 was not found; installing Python 3.14 for all users with winget."
    winget install --exact --id Python.Python.3.14 --scope machine `
        --accept-package-agreements --accept-source-agreements
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
        [Environment]::GetEnvironmentVariable("Path", "User")
}

function ConvertTo-CollectorUri {
    param([Parameter(Mandatory = $true)][string]$Value)
    $Text = $Value.Trim()
    if ($Text -notmatch "^[A-Za-z][A-Za-z0-9+.-]*://") { $Text = "https://" + $Text }
    try {
        $Uri = [Uri]$Text
    }
    catch {
        Fail "The server address is not a valid URL."
    }
    if ($Uri.Scheme -ne "https") { Fail "The server address must use HTTPS." }
    if (-not [string]::IsNullOrEmpty($Uri.UserInfo)) {
        Fail "The server address must not contain credentials."
    }
    if ($Uri.AbsolutePath -eq "/") {
        return $Uri.GetLeftPart([UriPartial]::Authority) + $DefaultIngestPath
    }
    return $Uri.GetLeftPart([UriPartial]::Path)
}

function Read-Token {
    param([string]$Path)
    if ($Path) {
        if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { Fail "Token file not found." }
        return (Get-Content -LiteralPath $Path -Raw).Trim()
    }
    $Secure = Read-Host "Agent API token (input is hidden)" -AsSecureString
    $Pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($Secure)
    try {
        return ([Runtime.InteropServices.Marshal]::PtrToStringBSTR($Pointer)).Trim()
    }
    finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($Pointer)
    }
}

function New-OwnerOnlyDirectory {
    $Path = Join-Path ([IO.Path]::GetTempPath()) ("monitor-agent-setup-" + [Guid]::NewGuid())
    $Directory = [IO.Directory]::CreateDirectory($Path)
    $Acl = New-Object Security.AccessControl.DirectorySecurity
    $Acl.SetAccessRuleProtection($true, $false)
    foreach ($Sid in @(
            [Security.Principal.WindowsIdentity]::GetCurrent().User.Value,
            "S-1-5-18"
        )) {
        $Rule = New-Object Security.AccessControl.FileSystemAccessRule(
            (New-Object Security.Principal.SecurityIdentifier($Sid)),
            [Security.AccessControl.FileSystemRights]::FullControl,
            ([Security.AccessControl.InheritanceFlags]::ContainerInherit -bor
                [Security.AccessControl.InheritanceFlags]::ObjectInherit),
            [Security.AccessControl.PropagationFlags]::None,
            [Security.AccessControl.AccessControlType]::Allow
        )
        $Acl.AddAccessRule($Rule)
    }
    Set-Acl -LiteralPath $Directory.FullName -AclObject $Acl
    return $Directory.FullName
}

function Read-EmployeesNotified {
    if ($NoEmployeesNotified) { return $true }
    if ($EmployeesNotified) { return $true }
    $ErrorActionPreference = "Continue"
    Write-Host ""
    Write-Host "Screenshots, active-window titles and file audit are sensitive telemetry."
    Write-Host "Collecting them without telling employees may be unlawful. Enable them only"
    Write-Host "after employees have been notified in line with local law and your policy."
    $Answer = Read-Host "Have employees been notified? Enable these collectors? [Y/n]"
    return ($Answer.Trim() -notmatch "^[nN]")
}

function Write-AgentConfiguration {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][hashtable]$Settings
    )
    $Applied = @{}
    $Lines = New-Object System.Collections.Generic.List[string]
    foreach ($Line in (Get-Content -LiteralPath $EnvironmentExample)) {
        $Key = $null
        if ($Line -match "^([A-Z0-9_]+)=") { $Key = $Matches[1] }
        if ($Key -and $Settings.ContainsKey($Key)) {
            if ($null -ne $Settings[$Key]) { $Lines.Add("$Key=$($Settings[$Key])") }
            $Applied[$Key] = $true
        }
        else {
            $Lines.Add($Line)
        }
    }
    foreach ($Key in $Settings.Keys) {
        if (-not $Applied.ContainsKey($Key) -and $null -ne $Settings[$Key]) {
            $Lines.Add("$Key=$($Settings[$Key])")
        }
    }
    $Encoding = New-Object System.Text.UTF8Encoding($false)
    [IO.File]::WriteAllLines($Path, $Lines, $Encoding)
}

function Test-CollectorConnection {
    param(
        [Parameter(Mandatory = $true)][string]$CollectorUri,
        [string]$CaBundle
    )
    $ErrorActionPreference = "Continue"
    if (-not (Get-Command curl.exe -ErrorAction SilentlyContinue)) { return }
    $Arguments = @("--silent", "--output", "NUL", "--max-time", "15", "--ssl-no-revoke")
    if ($CaBundle) { $Arguments += @("--cacert", $CaBundle) }
    $Arguments += $CollectorUri
    & curl.exe @Arguments
    if ($LASTEXITCODE -eq 0) {
        Write-Host "The server answered over verified HTTPS."
    }
    else {
        Write-Warning ("Could not reach the server over verified HTTPS (curl exit code " +
            "$LASTEXITCODE). Installing anyway; events stay queued until it is reachable.")
    }
}

$WorkDirectory = $null
try {
    Write-Host "== Monitor Agent setup =="
    Assert-Administrator

    $Wheel = Get-ChildItem -LiteralPath (Join-Path $PackageRoot "dist") `
        -Filter "monitor_agent-*.whl" -File -ErrorAction SilentlyContinue |
        Sort-Object Name | Select-Object -Last 1
    if ($null -eq $Wheel) { Fail "No monitor_agent wheel was found in the dist folder." }

    if (-not $ServerUrl) {
        $ServerUrl = Read-Host "Server address (for example https://monitor.example.com:8443)"
    }
    if ([string]::IsNullOrWhiteSpace($ServerUrl)) { Fail "A server address is required." }
    $CollectorUri = ConvertTo-CollectorUri $ServerUrl
    Write-Host "Collector URL: $CollectorUri"

    $Token = Read-Token $TokenFile
    if ([string]::IsNullOrWhiteSpace($Token) -or $Token -match "\s") {
        Fail "The API token is empty or contains whitespace."
    }

    if (-not $CaFile) {
        $BundledCa = Join-Path $PackageRoot "ca.crt"
        if (Test-Path -LiteralPath $BundledCa -PathType Leaf) { $CaFile = $BundledCa }
    }
    $CaBundle = $null
    if ($CaFile) {
        if (-not (Test-Path -LiteralPath $CaFile -PathType Leaf)) { Fail "CA file not found." }
        New-Item -ItemType Directory -Path $CaTargetDirectory -Force | Out-Null
        Copy-Item -LiteralPath $CaFile -Destination $CaTargetPath -Force
        $CaBundle = $CaTargetPath
        Write-Host "Private CA installed at $CaTargetPath"
    }

    $SelectedPython = Find-PythonVersion $PythonVersion
    if ($null -eq $SelectedPython) {
        Install-PythonWithWinget
        $SelectedPython = Find-PythonVersion $PythonVersion
    }
    if ($null -eq $SelectedPython) {
        Fail ("Install Python 3.11-3.14 for all users from https://www.python.org/downloads/ " +
            "with the py launcher, then run Install.cmd again.")
    }
    Write-Host "Using Python $SelectedPython"

    Test-CollectorConnection -CollectorUri $CollectorUri -CaBundle $CaBundle

    $Notified = Read-EmployeesNotified
    $Settings = @{
        "MONITOR_COLLECTOR_URI" = $CollectorUri
        "MONITOR_API_TOKEN"     = $Token
    }
    if ($CaBundle) { $Settings["MONITOR_CA_BUNDLE"] = $CaBundle }
    if ($Notified) {
        $AuditValue = if ($AuditPaths) { $AuditPaths } else { $DefaultAuditPaths }
        $Settings["MONITOR_EMPLOYEE_NOTICE_ACK"] = "true"
        $Settings["MONITOR_SCREENSHOT_ENABLED"] = "true"
        $Settings["MONITOR_INCLUDE_ACTIVE_WINDOW"] = "true"
        $Settings["MONITOR_AUDIT_PATHS"] = $AuditValue
        Write-Host ("Sensitive collection enabled: screenshots, active window, file audit (" +
            "$AuditValue).")
    }
    else {
        Write-Host "Sensitive collection left disabled (no employee notice acknowledged)."
    }

    $WorkDirectory = New-OwnerOnlyDirectory
    $ConfigurationPath = Join-Path $WorkDirectory "monitor-agent.env"
    Write-AgentConfiguration -Path $ConfigurationPath -Settings $Settings
    $Token = $null
    $Settings = $null

    $global:LASTEXITCODE = 0
    & $Installer -WheelPath $Wheel.FullName -EnvironmentFile $ConfigurationPath `
        -PythonVersion $SelectedPython
    if ($LASTEXITCODE -ne 0) { Fail "install.ps1 failed with exit code $LASTEXITCODE." }

    Write-Host ""
    Write-Host "Monitor Agent is installed. The machine appears on the server after its first event."
    Write-Host "Log: C:\ProgramData\MonitorAgent\logs\monitor-agent.log"
    $ExitCode = 0
}
catch {
    Write-Host ""
    Write-Host ("SETUP FAILED: " + $_.Exception.Message) -ForegroundColor Red
    $ExitCode = 1
}
finally {
    if ($WorkDirectory -and (Test-Path -LiteralPath $WorkDirectory)) {
        Remove-Item -LiteralPath $WorkDirectory -Recurse -Force
    }
}
exit $ExitCode
