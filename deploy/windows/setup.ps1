<#
Interactive Monitor Agent setup for Windows. Install.cmd runs it elevated.

Nothing secret ships with the package: the collector URL and API token are asked for at
install time (or read from -TokenFile), written to a temporary owner-only configuration,
handed to install.ps1 and deleted again. A private CA certificate is optional; when a
file named ca.crt sits next to Install.cmd (or -CaFile is given) it is installed under
C:\ProgramData\MonitorAgentCA and referenced through MONITOR_CA_BUNDLE.
#>
param(
    [string]$ServerUrl,
    [string]$TokenFile,
    [string]$CaFile,
    [ValidateSet("3.11", "3.12", "3.13", "3.14")]
    [string]$PythonVersion
)

$ErrorActionPreference = "Stop"
$ScriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$PackageRoot = Split-Path -Parent (Split-Path -Parent $ScriptRoot)
$Installer = Join-Path $ScriptRoot "install.ps1"
$EnvironmentExample = Join-Path $ScriptRoot "monitor-agent.env.example"
$CaTargetDirectory = "C:\ProgramData\MonitorAgentCA"
$CaTargetPath = Join-Path $CaTargetDirectory "ca.crt"
$DefaultIngestPath = "/api/v1/telemetry"
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

function Write-AgentConfiguration {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$CollectorUri,
        [Parameter(Mandatory = $true)][string]$Token,
        [string]$CaBundle
    )
    $Lines = New-Object System.Collections.Generic.List[string]
    foreach ($Line in (Get-Content -LiteralPath $EnvironmentExample)) {
        if ($Line.StartsWith("MONITOR_COLLECTOR_URI=")) {
            $Lines.Add("MONITOR_COLLECTOR_URI=$CollectorUri")
        }
        elseif ($Line.StartsWith("MONITOR_API_TOKEN=")) {
            $Lines.Add("MONITOR_API_TOKEN=$Token")
        }
        elseif ($Line.StartsWith("MONITOR_CA_BUNDLE=")) {
            continue
        }
        else {
            $Lines.Add($Line)
        }
    }
    if ($CaBundle) { $Lines.Add("MONITOR_CA_BUNDLE=$CaBundle") }
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

    $WorkDirectory = New-OwnerOnlyDirectory
    $ConfigurationPath = Join-Path $WorkDirectory "monitor-agent.env"
    Write-AgentConfiguration -Path $ConfigurationPath -CollectorUri $CollectorUri `
        -Token $Token -CaBundle $CaBundle
    $Token = $null

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
