# Migrate Monitor Agent v1 to v2

This procedure preserves a recoverable v1 deployment while you validate and switch to Monitor Agent 2.0.0. Run each platform section from an administrative shell. Do not replace the managed runtime until staging validation completes.

## 1. Create preflight recovery material

Before the upgrade, create an **external, owner-restricted backup** outside every managed Monitor Agent directory. Capture the existing executable or runtime, the service/task/LaunchDaemon definition, the protected environment file, and the observed enabled/running state. Use a fresh backup path for each change and abort if it already exists; never merge new recovery material into an older backup. A successful v2 installer cleans its internal transaction backups, so those temporary backups are not rollback material.

On Linux, capture the existing service state and artifacts before touching `/opt/monitor-agent`, `/etc/monitor-agent`, or `/var/lib/monitor-agent`:

```bash
if sudo test -e /root/monitor-agent-v1-backup; then
    printf '%s\n' 'backup path already exists; aborting' >&2
    exit 1
fi
sudo install -d -m 0700 /root/monitor-agent-v1-backup /root/monitor-agent-v1-backup/opt /root/monitor-agent-v1-backup/etc/systemd/system
enabled_status=0
enabled_state=$(sudo systemctl is-enabled monitor-agent.service 2>/dev/null) || enabled_status=$?
case "$enabled_status:$enabled_state" in
    0:enabled) prior_enabled=true ;;
    1:disabled) prior_enabled=false ;;
    *) printf '%s\n' 'unable to record prior enabled state; aborting' >&2; exit 1 ;;
esac
active_status=0
active_state=$(sudo systemctl is-active monitor-agent.service 2>/dev/null) || active_status=$?
case "$active_status:$active_state" in
    0:active) prior_active=true ;;
    3:inactive) prior_active=false ;;
    *) printf '%s\n' 'unable to record prior running state; aborting' >&2; exit 1 ;;
esac
printf 'enabled=%s\nactive=%s\n' "$prior_enabled" "$prior_active" | \
    sudo tee /root/monitor-agent-v1-backup/service-state.env >/dev/null
sudo cp -a /opt/monitor-agent /root/monitor-agent-v1-backup/opt/monitor-agent
sudo cp -a /etc/monitor-agent /root/monitor-agent-v1-backup/etc/monitor-agent
sudo cp -a /etc/systemd/system/monitor-agent.service /root/monitor-agent-v1-backup/etc/systemd/system/monitor-agent.service
```

On Windows, use an elevated PowerShell session to create an ACL-restricted external backup, export the task XML, preserve only the prior runtime, launcher, task XML, and protected environment file, and record its task state. This backup explicitly excludes the v2 spool and logs:

```powershell
$ErrorActionPreference = "Stop"

$backup = "C:\SecureBackups\MonitorAgentV1"
if (Test-Path -LiteralPath $backup) { throw "backup path already exists; aborting" }
New-Item -ItemType Directory -Path $backup | Out-Null
icacls "$backup" /inheritance:r /grant:r "*S-1-5-18:(OI)(CI)F" "*S-1-5-32-544:(OI)(CI)F"
if ($LASTEXITCODE -ne 0) { throw "backup ACL configuration failed" }
New-Item -ItemType Directory -Path "$backup\runtime", "$backup\configuration" | Out-Null
Export-ScheduledTask -TaskName MonitorAgent | Set-Content -Encoding UTF8 "$backup\monitor_agent_task.xml"
$taskService = New-Object -ComObject "Schedule.Service"
$taskService.Connect()
$registeredTask = $taskService.GetFolder("\").GetTask("MonitorAgent")
$taskEnabled = if ($registeredTask.Enabled) { "true" } else { "false" }
$taskStateCode = [int]$registeredTask.State
$taskRunning = if ($taskStateCode -eq 4) { "true" } else { "false" }
@(
    "enabled=$taskEnabled"
    "running=$taskRunning"
    "state_code=$taskStateCode"
) | Set-Content -Encoding ASCII "$backup\task-state.env"
Copy-Item -Recurse -Force C:\ProgramData\MonitorAgent\venv "$backup\runtime\venv"
Copy-Item -Force C:\ProgramData\MonitorAgent\run-agent.ps1, C:\ProgramData\MonitorAgent\monitor_agent_task.xml "$backup\runtime"
Copy-Item -Force C:\ProgramData\MonitorAgent\monitor-agent.env "$backup\configuration\monitor-agent.env"
```

On macOS, record the LaunchDaemon state and copy only the exact application runtime, launcher, protected environment file, and plist into an owner-restricted external backup. This backup explicitly excludes the v2 spool and logs:

```bash
if sudo test -e /var/root/MonitorAgentV1Backup; then
    printf '%s\n' 'backup path already exists; aborting' >&2
    exit 1
fi
sudo install -d -m 0700 /var/root/MonitorAgentV1Backup /var/root/MonitorAgentV1Backup/runtime /var/root/MonitorAgentV1Backup/configuration /var/root/MonitorAgentV1Backup/LaunchDaemons
launchd_status=0
launchd_print=$(sudo launchctl print system/com.company.monitor-agent 2>&1) || launchd_status=$?
case "$launchd_status" in
    0)
        prior_loaded=true
        case "$launchd_print" in
            *"state = running"*) prior_running=true ;;
            *) prior_running=false ;;
        esac
        ;;
    113)
        prior_loaded=false
        prior_running=false
        ;;
    *) printf '%s\n' 'unable to record prior LaunchDaemon state; aborting' >&2; exit 1 ;;
esac
disabled_output=$(sudo launchctl print-disabled system) || {
    printf '%s\n' 'unable to record prior LaunchDaemon enable state; aborting' >&2
    exit 1
}
case "$disabled_output" in
    *'"com.company.monitor-agent" => true'*) prior_disabled=true ;;
    *'"com.company.monitor-agent" => false'*) prior_disabled=false ;;
    *) prior_disabled=false ;;
esac
printf 'loaded=%s\nrunning=%s\ndisabled=%s\n' \
    "$prior_loaded" "$prior_running" "$prior_disabled" | \
    sudo tee /var/root/MonitorAgentV1Backup/launchd-state.env >/dev/null
sudo cp -a "/Library/Application Support/MonitorAgent/venv" /var/root/MonitorAgentV1Backup/runtime/venv
sudo cp -a "/Library/Application Support/MonitorAgent/run-agent.sh" /var/root/MonitorAgentV1Backup/runtime/run-agent.sh
sudo cp -a "/Library/Application Support/MonitorAgent/monitor-agent.env" /var/root/MonitorAgentV1Backup/configuration/monitor-agent.env
sudo cp -a /Library/LaunchDaemons/com.company.monitor-agent.plist /var/root/MonitorAgentV1Backup/LaunchDaemons/com.company.monitor-agent.plist
```

## 2. Build and verify the release input

Build the exact release wheel from the reviewed source:

```bash
python -m build
sha256sum dist/monitor_agent-2.0.0-py3-none-any.whl
python -m twine check dist/*
```

Record the checksum with the change record. On Windows, use `Get-FileHash -Algorithm SHA256`; on macOS, use `shasum -a 256`. Compare the result with the approved release checksum before installing.

## 3. Validate in a staging virtual environment

Create a side-by-side staging virtual environment outside the managed service root. The commands in each platform block assume the approved secret manager has injected the collector URI and API token into the current root shell on Linux or macOS, or the current elevated PowerShell session on Windows. Do not source a service environment file as shell code, and do not expose a token in command history. Give staging its own owner-only spool and log paths.

On Linux:

```bash
if test -e /var/lib/monitor-agent-v2-staging; then
    printf '%s\n' 'staging path already exists; aborting' >&2
    exit 1
fi
sudo install -d -m 0700 /var/lib/monitor-agent-v2-staging
sudo python3.14 -m venv /var/lib/monitor-agent-v2-staging/venv
sudo /var/lib/monitor-agent-v2-staging/venv/bin/python -m pip install dist/monitor_agent-2.0.0-py3-none-any.whl
export MONITOR_SPOOL_PATH=/var/lib/monitor-agent-v2-staging/spool
export MONITOR_LOG_PATH=/var/lib/monitor-agent-v2-staging/monitor-agent.log
/var/lib/monitor-agent-v2-staging/venv/bin/monitor-agent check-config
/var/lib/monitor-agent-v2-staging/venv/bin/monitor-agent once --event heartbeat --no-transmit
```

On Windows, use an elevated PowerShell session and an ACL-restricted staging location:

```powershell
$staging = "C:\SecureStaging\monitor-agent-v2"
if (Test-Path -LiteralPath $staging) { throw "staging path already exists; aborting" }
New-Item -ItemType Directory -Path $staging | Out-Null
icacls "$staging" /inheritance:r /grant:r "*S-1-5-18:(OI)(CI)F" "*S-1-5-32-544:(OI)(CI)F"
if ($LASTEXITCODE -ne 0) { throw "staging ACL configuration failed" }
py -3.14 -m venv "$staging\venv"
& "$staging\venv\Scripts\python.exe" -m pip install .\dist\monitor_agent-2.0.0-py3-none-any.whl
$env:MONITOR_SPOOL_PATH = "$staging\spool"
$env:MONITOR_LOG_PATH = "$staging\monitor-agent.log"
& "$staging\venv\Scripts\monitor-agent.exe" check-config
& "$staging\venv\Scripts\monitor-agent.exe" once --event heartbeat --no-transmit
```

On macOS:

```bash
if test -e /var/root/MonitorAgentV2Staging; then
    printf '%s\n' 'staging path already exists; aborting' >&2
    exit 1
fi
sudo install -d -m 0700 /var/root/MonitorAgentV2Staging
sudo python3.14 -m venv /var/root/MonitorAgentV2Staging/venv
sudo /var/root/MonitorAgentV2Staging/venv/bin/python -m pip install dist/monitor_agent-2.0.0-py3-none-any.whl
export MONITOR_SPOOL_PATH=/var/root/MonitorAgentV2Staging/spool
export MONITOR_LOG_PATH=/var/root/MonitorAgentV2Staging/monitor-agent.log
/var/root/MonitorAgentV2Staging/venv/bin/monitor-agent check-config
/var/root/MonitorAgentV2Staging/venv/bin/monitor-agent once --event heartbeat --no-transmit
```

Run the validation commands from the same privileged shell that received the protected transport variables. `check-config` must print `configuration valid`. The no-transmit event must contain `schema_version` `1.0`, a UUID event identifier, every legacy payload section, and `agent.version` `2.0.0` without a token or raw platform identifier.

## 4. Switch the managed service

Use the platform installer with the verified wheel and protected environment file. The installers are transactional while they run and restore their prior managed state if an installation step fails.

Linux:

```bash
sudo deploy/linux/install.sh dist/monitor_agent-2.0.0-py3-none-any.whl /secure/path/monitor-agent.env
sudo systemctl status monitor-agent.service
sudo journalctl -u monitor-agent.service
```

Windows, from elevated PowerShell:

```powershell
.\deploy\windows\install.ps1 -WheelPath .\dist\monitor_agent-2.0.0-py3-none-any.whl -EnvironmentFile C:\secure\monitor-agent.env -PythonVersion 3.14
Get-ScheduledTask -TaskName MonitorAgent
Get-ScheduledTaskInfo -TaskName MonitorAgent
```

macOS:

```bash
sudo deploy/macos/install.sh dist/monitor_agent-2.0.0-py3-none-any.whl /secure/path/monitor-agent.env
sudo launchctl print system/com.company.monitor-agent
```

Verify the service starts and observe the first heartbeat. Confirm the protected v2 spool exists and remains owner-only. Do not use a purge uninstall as part of an upgrade or rollback.

## 5. Roll back only from the external backup

Rollback restores the previous executable/runtime path, protected environment, and service definition from the preflight backup. It does not delete the v2 spool. Preserve that spool for incident analysis or later recovery. Do not use purge during rollback.

### Linux rollback

Stop the v2 service, restore the saved runtime, environment, and unit file, then reload and restore the recorded enabled/running state:

```bash
set -eu

backup=/root/monitor-agent-v1-backup
state_file="$backup/service-state.env"
sudo test -f "$state_file" || { printf '%s\n' 'missing saved service state; aborting' >&2; exit 1; }
state_lines=$(sudo awk 'END { print NR }' "$state_file")
if [ "$state_lines" -ne 2 ] || \
    [ "$(sudo grep -Ec '^enabled=(true|false)$' "$state_file")" -ne 1 ] || \
    [ "$(sudo grep -Ec '^active=(true|false)$' "$state_file")" -ne 1 ]; then
    printf '%s\n' 'invalid saved service state; aborting' >&2
    exit 1
fi
prior_enabled=$(sudo sed -n -E 's/^enabled=(true|false)$/\1/p' "$state_file")
prior_active=$(sudo sed -n -E 's/^active=(true|false)$/\1/p' "$state_file")
case "$prior_enabled:$prior_active" in
    true:true|true:false|false:true|false:false) ;;
    *) printf '%s\n' 'invalid saved service state; aborting' >&2; exit 1 ;;
esac
sudo test -d "$backup/opt/monitor-agent" || { printf '%s\n' 'missing saved runtime; aborting' >&2; exit 1; }
sudo test -f "$backup/etc/monitor-agent/monitor-agent.env" || { printf '%s\n' 'missing saved environment; aborting' >&2; exit 1; }
sudo test -f "$backup/etc/systemd/system/monitor-agent.service" || { printf '%s\n' 'missing saved unit; aborting' >&2; exit 1; }
sudo test -d /opt/monitor-agent || { printf '%s\n' 'missing v2 runtime; aborting' >&2; exit 1; }
sudo test -f /etc/monitor-agent/monitor-agent.env || { printf '%s\n' 'missing v2 environment; aborting' >&2; exit 1; }
sudo test -f /etc/systemd/system/monitor-agent.service || { printf '%s\n' 'missing v2 unit; aborting' >&2; exit 1; }
if sudo test -e /root/monitor-agent-v2-displaced; then
    printf '%s\n' 'v2 recovery path already exists; aborting' >&2
    exit 1
fi
sudo install -d -m 0700 /root/monitor-agent-v2-displaced
sudo systemctl stop monitor-agent.service
sudo mv /opt/monitor-agent /root/monitor-agent-v2-displaced/monitor-agent
sudo mv /etc/monitor-agent/monitor-agent.env /root/monitor-agent-v2-displaced/monitor-agent.env
sudo mv /etc/systemd/system/monitor-agent.service /root/monitor-agent-v2-displaced/monitor-agent.service
sudo cp -a "$backup/opt/monitor-agent" /opt/
sudo install -d -m 0700 /etc/monitor-agent
sudo cp -a "$backup/etc/monitor-agent/monitor-agent.env" /etc/monitor-agent/
sudo cp -a "$backup/etc/systemd/system/monitor-agent.service" /etc/systemd/system/
sudo systemctl daemon-reload
case "$prior_enabled" in
    true) sudo systemctl enable monitor-agent.service ;;
    false) sudo systemctl disable monitor-agent.service ;;
esac
case "$prior_active" in
    true) sudo systemctl start monitor-agent.service ;;
    false) sudo systemctl stop monitor-agent.service ;;
esac
```

The saved booleans are the source of truth: the rollback branches on each one and
does not infer service state from the new v2 installation.

### Windows rollback

From elevated PowerShell, stop and unregister the v2 task, restore the prior runtime and protected environment from the external backup, then restore the saved task definition and its prior state:

```powershell
$ErrorActionPreference = "Stop"

$backup = "C:\SecureBackups\MonitorAgentV1"
$statePath = "$backup\task-state.env"
if (-not (Test-Path -LiteralPath $statePath -PathType Leaf)) {
    throw "saved task state is missing"
}
$stateLines = @(Get-Content -LiteralPath $statePath)
$priorState = ConvertFrom-StringData -String (Get-Content -LiteralPath $statePath -Raw)
if ($stateLines.Count -ne 3 -or
    $priorState.Keys.Count -ne 3 -or
    ((@($priorState.Keys | Sort-Object) -join ",") -ne "enabled,running,state_code") -or
    $priorState.enabled -notin @("true", "false") -or
    $priorState.running -notin @("true", "false") -or
    $priorState.state_code -notmatch '^[0-9]+$') {
    throw "saved task state is invalid"
}
$requiredBackupPaths = @(
    [pscustomobject]@{ Path = "$backup\monitor_agent_task.xml"; Type = "Leaf" },
    [pscustomobject]@{ Path = "$backup\runtime\venv"; Type = "Container" },
    [pscustomobject]@{ Path = "$backup\runtime\run-agent.ps1"; Type = "Leaf" },
    [pscustomobject]@{ Path = "$backup\runtime\monitor_agent_task.xml"; Type = "Leaf" },
    [pscustomobject]@{ Path = "$backup\configuration\monitor-agent.env"; Type = "Leaf" }
)
foreach ($requiredPath in $requiredBackupPaths) {
    if (-not (Test-Path -LiteralPath $requiredPath.Path -PathType $requiredPath.Type)) {
        throw "saved task recovery material is missing or has the wrong type"
    }
}
$requiredV2Paths = @(
    [pscustomobject]@{ Path = "C:\ProgramData\MonitorAgent\venv"; Type = "Container" },
    [pscustomobject]@{ Path = "C:\ProgramData\MonitorAgent\run-agent.ps1"; Type = "Leaf" },
    [pscustomobject]@{ Path = "C:\ProgramData\MonitorAgent\monitor_agent_task.xml"; Type = "Leaf" },
    [pscustomobject]@{ Path = "C:\ProgramData\MonitorAgent\monitor-agent.env"; Type = "Leaf" }
)
foreach ($requiredPath in $requiredV2Paths) {
    if (-not (Test-Path -LiteralPath $requiredPath.Path -PathType $requiredPath.Type)) {
        throw "current v2 recovery material is missing or has the wrong type"
    }
}
$currentTask = Get-ScheduledTask -TaskName MonitorAgent -ErrorAction Stop
if ($currentTask.TaskName -ne "MonitorAgent") {
    throw "current v2 task identity is invalid"
}
$displaced = "C:\SecureBackups\MonitorAgentV2Displaced"
if (Test-Path -LiteralPath $displaced) { throw "v2 recovery path already exists; aborting" }
New-Item -ItemType Directory -Path $displaced | Out-Null
icacls "$displaced" /inheritance:r /grant:r "*S-1-5-18:(OI)(CI)F" "*S-1-5-32-544:(OI)(CI)F"
if ($LASTEXITCODE -ne 0) { throw "v2 recovery ACL configuration failed" }
if ([int]$currentTask.State -eq 4) { Stop-ScheduledTask -TaskName MonitorAgent }
Unregister-ScheduledTask -TaskName MonitorAgent -Confirm:$false
Move-Item -LiteralPath C:\ProgramData\MonitorAgent\venv -Destination "$displaced\venv"
Move-Item -LiteralPath C:\ProgramData\MonitorAgent\run-agent.ps1 -Destination "$displaced\run-agent.ps1"
Move-Item -LiteralPath C:\ProgramData\MonitorAgent\monitor_agent_task.xml -Destination "$displaced\monitor_agent_task.xml"
Move-Item -LiteralPath C:\ProgramData\MonitorAgent\monitor-agent.env -Destination "$displaced\monitor-agent.env"
Copy-Item -Recurse -Force "$backup\runtime\venv" C:\ProgramData\MonitorAgent\venv
Copy-Item -Force "$backup\runtime\run-agent.ps1", "$backup\runtime\monitor_agent_task.xml" C:\ProgramData\MonitorAgent
Copy-Item -Force "$backup\configuration\monitor-agent.env" C:\ProgramData\MonitorAgent\monitor-agent.env
Register-ScheduledTask -TaskName MonitorAgent -Xml (Get-Content "$backup\monitor_agent_task.xml" -Raw) -Force
if ($priorState.running -eq "true") {
    if ($priorState.enabled -eq "false") { Enable-ScheduledTask -TaskName MonitorAgent }
    Start-ScheduledTask -TaskName MonitorAgent
} else {
    Stop-ScheduledTask -TaskName MonitorAgent -ErrorAction SilentlyContinue
}
if ($priorState.enabled -eq "true") {
    Enable-ScheduledTask -TaskName MonitorAgent
} else {
    Disable-ScheduledTask -TaskName MonitorAgent
}
$restoredTask = Get-ScheduledTask -TaskName MonitorAgent
if ([bool]$restoredTask.Enabled -ne [bool]::Parse($priorState.enabled) -or
    (([int]$restoredTask.State -eq 4) -ne [bool]::Parse($priorState.running))) {
    throw "restored task state does not match backup"
}
```

The state file uses scheduler booleans and an integer state code, not formatted or
localized command output. The displaced v2 runtime is retained separately; its
spool is never moved or deleted.

### macOS rollback

Unload the v2 LaunchDaemon, restore the backed-up runtime, environment, and plist, then load the prior definition and restore its recorded running state:

```bash
set -eu

backup=/var/root/MonitorAgentV1Backup
state_file="$backup/launchd-state.env"
sudo test -f "$state_file" || { printf '%s\n' 'missing saved LaunchDaemon state; aborting' >&2; exit 1; }
state_lines=$(sudo awk 'END { print NR }' "$state_file")
if [ "$state_lines" -ne 3 ] || \
    [ "$(sudo grep -Ec '^loaded=(true|false)$' "$state_file")" -ne 1 ] || \
    [ "$(sudo grep -Ec '^running=(true|false)$' "$state_file")" -ne 1 ] || \
    [ "$(sudo grep -Ec '^disabled=(true|false)$' "$state_file")" -ne 1 ]; then
    printf '%s\n' 'invalid saved LaunchDaemon state; aborting' >&2
    exit 1
fi
prior_loaded=$(sudo sed -n -E 's/^loaded=(true|false)$/\1/p' "$state_file")
prior_running=$(sudo sed -n -E 's/^running=(true|false)$/\1/p' "$state_file")
prior_disabled=$(sudo sed -n -E 's/^disabled=(true|false)$/\1/p' "$state_file")
case "$prior_loaded:$prior_running" in
    true:true|true:false|false:false) ;;
    *) printf '%s\n' 'invalid saved LaunchDaemon state; aborting' >&2; exit 1 ;;
esac
case "$prior_disabled" in
    true|false) ;;
    *) printf '%s\n' 'invalid saved LaunchDaemon enable state; aborting' >&2; exit 1 ;;
esac
sudo test -d "$backup/runtime/venv" || { printf '%s\n' 'missing saved runtime; aborting' >&2; exit 1; }
sudo test -f "$backup/runtime/run-agent.sh" || { printf '%s\n' 'missing saved launcher; aborting' >&2; exit 1; }
sudo test -f "$backup/configuration/monitor-agent.env" || { printf '%s\n' 'missing saved environment; aborting' >&2; exit 1; }
sudo test -f "$backup/LaunchDaemons/com.company.monitor-agent.plist" || { printf '%s\n' 'missing saved plist; aborting' >&2; exit 1; }
sudo test -d "/Library/Application Support/MonitorAgent/venv" || { printf '%s\n' 'missing v2 runtime; aborting' >&2; exit 1; }
sudo test -f "/Library/Application Support/MonitorAgent/run-agent.sh" || { printf '%s\n' 'missing v2 launcher; aborting' >&2; exit 1; }
sudo test -f "/Library/Application Support/MonitorAgent/monitor-agent.env" || { printf '%s\n' 'missing v2 environment; aborting' >&2; exit 1; }
sudo test -f /Library/LaunchDaemons/com.company.monitor-agent.plist || { printf '%s\n' 'missing v2 plist; aborting' >&2; exit 1; }
current_launchd_output=$(sudo launchctl print system/com.company.monitor-agent 2>&1) || {
    printf '%s\n' 'unable to validate current v2 LaunchDaemon; aborting' >&2
    exit 1
}
case "$current_launchd_output" in
    *"state = "*) ;;
    *) printf '%s\n' 'invalid current v2 LaunchDaemon state; aborting' >&2; exit 1 ;;
esac

read_backup_plist_boolean() {
    plist_key=$1
    if plist_value=$(sudo /usr/libexec/PlistBuddy -c "Print :$plist_key" \
        "$backup/LaunchDaemons/com.company.monitor-agent.plist" 2>/dev/null); then
        case "$plist_value" in
            true|false) printf '%s\n' "$plist_value" ;;
            *) printf '%s\n' invalid ;;
        esac
    else
        printf '%s\n' false
    fi
}

wait_for_launchdaemon_stopped() {
    attempts=0
    while [ "$attempts" -lt 10 ]; do
        restored_launchd_output=$(sudo launchctl print system/com.company.monitor-agent 2>&1) || {
            printf '%s\n' 'restored LaunchDaemon unloaded unexpectedly; manual recovery required' >&2
            exit 1
        }
        case "$restored_launchd_output" in
            *"state = running"*)
                sleep 1
                attempts=$((attempts + 1))
                ;;
            *) return 0 ;;
        esac
    done
    printf '%s\n' 'restored LaunchDaemon did not stop; manual recovery required' >&2
    exit 1
}

case "$prior_loaded:$prior_running:$prior_disabled" in
    true:false:*)
        backup_keep_alive=$(read_backup_plist_boolean KeepAlive)
        backup_run_at_load=$(read_backup_plist_boolean RunAtLoad)
        case "$backup_keep_alive:$backup_run_at_load" in
            true:*|*:true|invalid:*|*:invalid)
                printf '%s\n' 'cannot safely restore loaded, enabled, inactive LaunchDaemon; aborting' >&2
                exit 1
                ;;
        esac
        ;;
esac
if sudo test -e /var/root/MonitorAgentV2Displaced; then
    printf '%s\n' 'v2 recovery path already exists; aborting' >&2
    exit 1
fi
sudo install -d -m 0700 /var/root/MonitorAgentV2Displaced
if sudo launchctl print system/com.company.monitor-agent >/dev/null 2>&1; then
    sudo launchctl bootout system/com.company.monitor-agent
fi
sudo mv "/Library/Application Support/MonitorAgent/venv" /var/root/MonitorAgentV2Displaced/venv
sudo mv "/Library/Application Support/MonitorAgent/run-agent.sh" /var/root/MonitorAgentV2Displaced/run-agent.sh
sudo mv "/Library/Application Support/MonitorAgent/monitor-agent.env" /var/root/MonitorAgentV2Displaced/monitor-agent.env
sudo mv /Library/LaunchDaemons/com.company.monitor-agent.plist /var/root/MonitorAgentV2Displaced/com.company.monitor-agent.plist
sudo cp -a "$backup/runtime/venv" "/Library/Application Support/MonitorAgent/venv"
sudo cp -a "$backup/runtime/run-agent.sh" "/Library/Application Support/MonitorAgent/run-agent.sh"
sudo cp -a "$backup/configuration/monitor-agent.env" "/Library/Application Support/MonitorAgent/monitor-agent.env"
sudo cp -a "$backup/LaunchDaemons/com.company.monitor-agent.plist" /Library/LaunchDaemons/com.company.monitor-agent.plist
case "$prior_loaded" in
    true) sudo launchctl enable system/com.company.monitor-agent ;;
    false) ;;
esac
case "$prior_loaded:$prior_running" in
    true:true)
        sudo launchctl bootstrap system /Library/LaunchDaemons/com.company.monitor-agent.plist
        sudo launchctl kickstart -k system/com.company.monitor-agent
        ;;
    true:false)
        sudo launchctl bootstrap system /Library/LaunchDaemons/com.company.monitor-agent.plist
        if [ "$prior_disabled" = true ]; then
            sudo launchctl disable system/com.company.monitor-agent
        fi
        wait_for_launchdaemon_stopped
        ;;
    false:false) ;;
esac
case "$prior_disabled" in
    true) sudo launchctl disable system/com.company.monitor-agent ;;
    false) sudo launchctl enable system/com.company.monitor-agent ;;
esac
```

The saved loaded, running, and disabled booleans are applied independently. Keep
the v2 spool intact in all rollback cases; do not purge or remove its directory.
