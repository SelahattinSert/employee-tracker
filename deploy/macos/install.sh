#!/bin/sh
set -eu

fail() {
    printf '%s\n' "$1" >&2
    exit "${2:-1}"
}

if [ "$(id -u)" -ne 0 ]; then
    fail "monitor-agent install: root privileges required" 2
fi
if [ "$#" -ne 2 ]; then
    fail "monitor-agent install: expected WHEEL_PATH ENV_FILE" 2
fi

wheel_path=$1
environment_path=$2
script_dir=$(CDPATH=''; export CDPATH; cd -P "$(dirname "$0")" && pwd -P) ||
    fail "monitor-agent install: invalid deployment files"
project_root=$(CDPATH=''; export CDPATH; cd -P "$script_dir/../.." && pwd -P) ||
    fail "monitor-agent install: invalid deployment files"
requirements_path="$project_root/requirements.lock"
launcher_source="$script_dir/run-agent.sh"
plist_source="$script_dir/com.company.monitor-agent.plist"
python_command=${MONITOR_AGENT_PYTHON:-python3.11}

install_root="/Library/Application Support/MonitorAgent"
app_parent="/Library/Application Support"
log_root="/Library/Logs/MonitorAgent"
log_parent="/Library/Logs"
spool_root="$install_root/spool"
plist_parent="/Library/LaunchDaemons"
plist_target="/Library/LaunchDaemons/com.company.monitor-agent.plist"
launchd_label=com.company.monitor-agent

case "$wheel_path" in
    *.whl) ;;
    *) fail "monitor-agent install: invalid wheel" ;;
esac
for input_file in "$wheel_path" "$environment_path" "$requirements_path" "$launcher_source" "$plist_source"; do
    if [ ! -f "$input_file" ] || [ -L "$input_file" ]; then
        fail "monitor-agent install: invalid deployment input"
    fi
done
if ! command -v "install" >/dev/null 2>&1 ||
    ! command -v "stat" >/dev/null 2>&1 ||
    ! command -v "chown" >/dev/null 2>&1 ||
    ! command -v "chmod" >/dev/null 2>&1 ||
    ! command -v "plutil" >/dev/null 2>&1 ||
    ! command -v "launchctl" >/dev/null 2>&1 ||
    ! command -v "mktemp" >/dev/null 2>&1 ||
    ! command -v "mv" >/dev/null 2>&1 ||
    ! command -v "cp" >/dev/null 2>&1 ||
    ! command -v "rm" >/dev/null 2>&1; then
    fail "monitor-agent install: required command unavailable"
fi
if ! command -v "$python_command" >/dev/null 2>&1; then
    fail "monitor-agent install: Python interpreter not found"
fi
python_version=$("$python_command" -c 'import platform,sys; print(platform.python_implementation(), sys.version_info[0], sys.version_info[1])') ||
    fail "monitor-agent install: unable to inspect Python interpreter"
IFS=' ' read -r implementation major minor extra <<EOF
$python_version
EOF
case "${implementation:-}:${major:-}:${minor:-}:${extra:-}" in
    CPython:3:11: | CPython:3:12: | CPython:3:13: | CPython:3:14:) ;;
    *) fail "monitor-agent install: unsupported Python interpreter" ;;
esac

assert_safe_directory_chain() {
    safe_path=$1
    safe_current=/
    safe_remaining=${safe_path#/}
    while [ -n "$safe_remaining" ]; do
        safe_part=${safe_remaining%%/*}
        case "$safe_remaining" in
            */*) safe_remaining=${safe_remaining#*/} ;;
            *) safe_remaining= ;;
        esac
        safe_current="$safe_current$safe_part"
        if [ -L "$safe_current" ]; then
            fail "monitor-agent install: unsafe deployment target"
        fi
        if [ -e "$safe_current" ] && [ ! -d "$safe_current" ]; then
            fail "monitor-agent install: unsafe deployment target"
        fi
        safe_current="$safe_current/"
    done
}

assert_safe_regular_target() {
    safe_target=$1
    if [ -L "$safe_target" ] || { [ -e "$safe_target" ] && [ ! -f "$safe_target" ]; }; then
        fail "monitor-agent install: unsafe deployment target"
    fi
}

verify_owner_mode() {
    verified_path=$1
    verified_mode=$2
    verified_metadata=$(stat -f '%Su:%Sg:%Lp' "$verified_path" 2>/dev/null) ||
        fail "monitor-agent install: ownership verification failed"
    if [ "$verified_metadata" != "root:wheel:$verified_mode" ]; then
        fail "monitor-agent install: ownership verification failed"
    fi
}

ensure_secure_directory() {
    secure_path=$1
    if [ ! -e "$secure_path" ]; then
        install -d -m 0700 "$secure_path" || fail "monitor-agent install: directory creation failed"
        chown root:wheel "$secure_path" || fail "monitor-agent install: ownership update failed"
        chmod 0700 "$secure_path" || fail "monitor-agent install: mode update failed"
    fi
    if [ ! -d "$secure_path" ] || [ -L "$secure_path" ]; then
        fail "monitor-agent install: unsafe deployment target"
    fi
    verify_owner_mode "$secure_path" 700
}

read_launchd_state() {
    if launchd_output=$(launchctl print "system/$launchd_label" 2>/dev/null); then
        launchd_loaded=1
        case "$launchd_output" in
            *"state = running"*) launchd_running=1 ;;
            *) launchd_running=0 ;;
        esac
        return 0
    else
        launchd_status=$?
    fi
    if [ "$launchd_status" -eq 113 ]; then
        launchd_loaded=0
        launchd_running=0
        return 0
    fi
    return 1
}

for protected_path in "$app_parent" "$install_root" "$spool_root" "$log_parent" "$log_root" "$plist_parent"; do
    assert_safe_directory_chain "$protected_path"
done
assert_safe_regular_target "$plist_target"
for required_parent in "$app_parent" "$log_parent" "$plist_parent"; do
    if [ ! -d "$required_parent" ] || [ -L "$required_parent" ]; then
        fail "monitor-agent install: unsafe deployment target"
    fi
done
if [ -e "$install_root/venv" ] || [ -L "$install_root/venv" ]; then
    if [ ! -d "$install_root/venv" ] || [ -L "$install_root/venv" ]; then
        fail "monitor-agent install: unsafe deployment target"
    fi
fi
for regular_target in "$install_root/run-agent.sh" "$install_root/monitor-agent.env"; do
    if [ -e "$regular_target" ] || [ -L "$regular_target" ]; then
        if [ ! -f "$regular_target" ] || [ -L "$regular_target" ]; then
            fail "monitor-agent install: unsafe deployment target"
        fi
    fi
done
if ! read_launchd_state; then
    fail "monitor-agent install: unable to inspect LaunchDaemon state"
fi
prior_loaded=$launchd_loaded
prior_running=$launchd_running
if [ "$prior_loaded" -eq 1 ] &&
    { [ ! -f "$plist_target" ] || [ -L "$plist_target" ]; }; then
    fail "monitor-agent install: unsafe deployment target"
fi
if [ -d "$install_root" ]; then prior_install_root=1; else prior_install_root=0; fi
if [ -d "$log_root" ]; then prior_log_root=1; else prior_log_root=0; fi
if [ -d "$spool_root" ]; then prior_spool_root=1; else prior_spool_root=0; fi
if [ -d "$install_root/venv" ]; then prior_venv=1; else prior_venv=0; fi
if [ -f "$install_root/run-agent.sh" ]; then prior_launcher=1; else prior_launcher=0; fi
if [ -f "$install_root/monitor-agent.env" ]; then prior_environment=1; else prior_environment=0; fi
if [ -f "$plist_target" ]; then prior_plist=1; else prior_plist=0; fi

transaction_dir=
backup_root=
staged_root=
staged_plist=
mutation_started=0
daemon_state_mutated=0
committed=0
venv_backup_completed=0
launcher_backup_completed=0
environment_backup_completed=0
plist_backup_completed=0
venv_publication_completed=0
launcher_publication_completed=0
environment_publication_completed=0
plist_publication_completed=0

restore_launchdaemon() {
    restore_failed=0
    if [ "$prior_loaded" -eq 1 ]; then
        if ! launchctl bootstrap system "$plist_target" >/dev/null 2>&1; then
            restore_failed=1
        elif ! launchctl enable "system/$launchd_label" >/dev/null 2>&1; then
            restore_failed=1
        fi
        if [ "$prior_running" -eq 0 ] &&
            ! launchctl stop "$launchd_label" >/dev/null 2>&1; then
            restore_failed=1
        fi
        if ! read_launchd_state || [ "$launchd_loaded" -ne 1 ] ||
            [ "$launchd_running" -ne "$prior_running" ]; then
            restore_failed=1
        fi
    fi
    [ "$restore_failed" -eq 0 ]
}

stop_current_launchdaemon() {
    if [ "$daemon_state_mutated" -eq 0 ]; then
        return 0
    fi
    if ! read_launchd_state; then
        return 1
    fi
    if [ "$launchd_loaded" -eq 1 ]; then
        launchctl bootout "system/$launchd_label" >/dev/null 2>&1 || return 1
    fi
    return 0
}

restore_component() {
    restore_live=$1
    restore_backup=$2
    restore_prior=$3
    restore_backup_completed=$4
    restore_publication_completed=$5
    restore_component_failed=0

    if [ "$restore_backup_completed" -eq 1 ]; then
        if [ "$restore_publication_completed" -eq 1 ]; then
            if [ -e "$restore_live" ] || [ -L "$restore_live" ]; then
                rm -rf "$restore_live" >/dev/null 2>&1 ||
                    restore_component_failed=1
            fi
        elif [ -e "$restore_live" ] || [ -L "$restore_live" ]; then
            restore_component_failed=1
        fi
        if [ "$restore_component_failed" -eq 0 ]; then
            mv "$restore_backup" "$restore_live" >/dev/null 2>&1 ||
                restore_component_failed=1
        fi
    elif [ "$restore_publication_completed" -eq 1 ]; then
        if [ "$restore_prior" -eq 0 ]; then
            rm -rf "$restore_live" >/dev/null 2>&1 ||
                restore_component_failed=1
        else
            restore_component_failed=1
        fi
    fi

    [ "$restore_component_failed" -eq 0 ]
}

rollback() {
    rollback_failed=0
    if [ "$mutation_started" -eq 1 ]; then
        stop_current_launchdaemon || rollback_failed=1
        restore_component \
            "$install_root/venv" "$backup_root/venv" \
            "$prior_venv" "$venv_backup_completed" \
            "$venv_publication_completed" || rollback_failed=1
        restore_component \
            "$install_root/run-agent.sh" "$backup_root/run-agent.sh" \
            "$prior_launcher" "$launcher_backup_completed" \
            "$launcher_publication_completed" || rollback_failed=1
        restore_component \
            "$install_root/monitor-agent.env" "$backup_root/monitor-agent.env" \
            "$prior_environment" "$environment_backup_completed" \
            "$environment_publication_completed" || rollback_failed=1
        restore_component \
            "$plist_target" "$backup_root/com.company.monitor-agent.plist" \
            "$prior_plist" "$plist_backup_completed" \
            "$plist_publication_completed" || rollback_failed=1
        restore_launchdaemon || rollback_failed=1
        if [ "$prior_spool_root" -eq 0 ] && [ -d "$spool_root" ] &&
            [ ! -L "$spool_root" ]; then
            rmdir "$spool_root" >/dev/null 2>&1 || rollback_failed=1
        fi
        if [ "$prior_install_root" -eq 0 ] && [ -d "$install_root" ] &&
            [ ! -L "$install_root" ]; then
            rmdir "$install_root" >/dev/null 2>&1 || rollback_failed=1
        fi
        if [ "$prior_log_root" -eq 0 ] && [ -d "$log_root" ] &&
            [ ! -L "$log_root" ]; then
            rmdir "$log_root" >/dev/null 2>&1 || rollback_failed=1
        fi
    fi
    if [ "$rollback_failed" -ne 0 ]; then
        printf '%s\n' "monitor-agent install: rollback incomplete; recovery retained at $transaction_dir" >&2
        return 1
    fi
    return 0
}

cleanup() {
    original_status=$?
    trap - EXIT
    set +e
    if [ "$original_status" -ne 0 ] && [ "$committed" -eq 0 ]; then
        if rollback; then
            if [ -n "$transaction_dir" ] &&
                ! rm -rf "$transaction_dir" >/dev/null 2>&1; then
                printf '%s\n' "monitor-agent install: cleanup incomplete; recovery retained at $transaction_dir" >&2
            fi
        fi
        printf '%s\n' "monitor-agent install: installation failed" >&2
    fi
    if [ "$original_status" -eq 0 ] && [ -n "$transaction_dir" ]; then
        if ! rm -rf "$transaction_dir" >/dev/null 2>&1; then
            printf '%s\n' "monitor-agent install: cleanup incomplete; recovery retained at $transaction_dir" >&2
            exit 1
        fi
    fi
    exit "$original_status"
}
trap cleanup EXIT

transaction_dir=$(mktemp -d "$app_parent/.monitor-agent-transaction.XXXXXX") ||
    fail "monitor-agent install: transaction setup failed"
chown root:wheel "$transaction_dir" || fail "monitor-agent install: transaction setup failed"
chmod 0700 "$transaction_dir" || fail "monitor-agent install: transaction setup failed"
verify_owner_mode "$transaction_dir" 700
backup_root="$transaction_dir/backup"
install -d -m 0700 "$backup_root" || fail "monitor-agent install: transaction setup failed"
chown root:wheel "$backup_root" || fail "monitor-agent install: transaction setup failed"
chmod 0700 "$backup_root" || fail "monitor-agent install: transaction setup failed"
verify_owner_mode "$backup_root" 700

staged_root="$transaction_dir/runtime"
install -d -m 0700 "$staged_root" || fail "monitor-agent install: staging failed"
"$python_command" -m venv "$staged_root/venv" || fail "monitor-agent install: staging failed"
"$staged_root/venv/bin/python" -m pip install --require-hashes -r "$requirements_path" || fail "monitor-agent install: dependency installation failed"
"$staged_root/venv/bin/python" -m pip install --no-deps --force-reinstall "$wheel_path" || fail "monitor-agent install: wheel installation failed"
install -m 0700 "$launcher_source" "$staged_root/run-agent.sh" || fail "monitor-agent install: staging failed"
install -m 0600 "$environment_path" "$staged_root/monitor-agent.env" || fail "monitor-agent install: staging failed"
chown -R root:wheel "$staged_root" || fail "monitor-agent install: ownership update failed"
chmod 0700 "$staged_root" "$staged_root/venv" "$staged_root/run-agent.sh" || fail "monitor-agent install: mode update failed"
chmod 0600 "$staged_root/monitor-agent.env" || fail "monitor-agent install: mode update failed"
verify_owner_mode "$staged_root" 700
verify_owner_mode "$staged_root/run-agent.sh" 700
verify_owner_mode "$staged_root/monitor-agent.env" 600
staged_plist="$transaction_dir/com.company.monitor-agent.plist"
install -m 0644 "$plist_source" "$staged_plist" || fail "monitor-agent install: staging failed"
chown root:wheel "$staged_plist" || fail "monitor-agent install: ownership update failed"
verify_owner_mode "$staged_plist" 644
plutil -lint "$staged_plist" >/dev/null 2>&1 || fail "monitor-agent install: invalid LaunchDaemon plist"
install -d -m 0700 "$staged_root/.path-validation/spool" "$staged_root/.path-validation/logs" || fail "monitor-agent install: staging failed"
chown -R root:wheel "$staged_root/.path-validation" || fail "monitor-agent install: ownership update failed"
chmod 0700 "$staged_root/.path-validation" "$staged_root/.path-validation/spool" "$staged_root/.path-validation/logs" || fail "monitor-agent install: mode update failed"
"$staged_root/run-agent.sh" check-config >/dev/null 2>&1 || fail "monitor-agent install: staged configuration failed"

mutation_started=1
if [ "$prior_loaded" -eq 1 ]; then
    daemon_state_mutated=1
    launchctl bootout "system/$launchd_label" || fail "monitor-agent install: unable to stop LaunchDaemon"
fi
ensure_secure_directory "$install_root"
ensure_secure_directory "$log_root"
ensure_secure_directory "$spool_root"
if [ "$prior_venv" -eq 1 ]; then
    mv "$install_root/venv" "$backup_root/venv" ||
        fail "monitor-agent install: publish failed"
    venv_backup_completed=1
fi
if [ "$prior_launcher" -eq 1 ]; then
    mv "$install_root/run-agent.sh" "$backup_root/run-agent.sh" ||
        fail "monitor-agent install: publish failed"
    launcher_backup_completed=1
fi
if [ "$prior_environment" -eq 1 ]; then
    mv "$install_root/monitor-agent.env" "$backup_root/monitor-agent.env" ||
        fail "monitor-agent install: publish failed"
    environment_backup_completed=1
fi
if [ "$prior_plist" -eq 1 ]; then
    mv "$plist_target" "$backup_root/com.company.monitor-agent.plist" || fail "monitor-agent install: publish failed"
    plist_backup_completed=1
fi
mv "$staged_root/venv" "$install_root/venv" || fail "monitor-agent install: publish failed"
venv_publication_completed=1
mv "$staged_root/run-agent.sh" "$install_root/run-agent.sh" || fail "monitor-agent install: publish failed"
launcher_publication_completed=1
mv "$staged_root/monitor-agent.env" "$install_root/monitor-agent.env" || fail "monitor-agent install: publish failed"
environment_publication_completed=1
mv "$staged_plist" "$plist_target" || fail "monitor-agent install: publish failed"
plist_publication_completed=1
verify_owner_mode "$install_root/run-agent.sh" 700
verify_owner_mode "$install_root/monitor-agent.env" 600
verify_owner_mode "$plist_target" 644
"$install_root/run-agent.sh" check-config >/dev/null 2>&1 || fail "monitor-agent install: live configuration failed"
daemon_state_mutated=1
launchctl bootstrap system "$plist_target" || fail "monitor-agent install: unable to bootstrap LaunchDaemon"
launchctl enable system/com.company.monitor-agent || fail "monitor-agent install: unable to enable LaunchDaemon"
if ! read_launchd_state || [ "$launchd_loaded" -ne 1 ] || [ "$launchd_running" -ne 1 ]; then
    fail "monitor-agent install: unable to verify LaunchDaemon"
fi
committed=1
printf '%s\n' "monitor-agent install: LaunchDaemon installed"
