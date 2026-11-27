<#
Uninstall step for the Monitor Agent GUI installer: stop and remove the scheduled tasks and
the runtime state. Inno Setup removes the Program Files payload itself. The spool and logs
under C:\ProgramData\MonitorAgent are left in place unless -Purge is given.
#>
param(
    [Parameter(Mandatory = $true)][string]$InstallDir,
    [switch]$Purge
)

$ErrorActionPreference = "Continue"

foreach ($task in @("MonitorAgent", "MonitorAgentSession")) {
    & schtasks.exe /End /TN $task 2>$null | Out-Null
    & schtasks.exe /Delete /TN $task /F 2>$null | Out-Null
}

if ($Purge) {
    foreach ($path in @("C:\ProgramData\MonitorAgent", "C:\ProgramData\MonitorAgentCA")) {
        if (Test-Path -LiteralPath $path) {
            Remove-Item -LiteralPath $path -Recurse -Force -ErrorAction SilentlyContinue
        }
    }
}

exit 0
