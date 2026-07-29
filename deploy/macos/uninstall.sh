#!/bin/sh
set -eu

fail() {
    printf '%s\n' "$1" >&2
    exit 2
}

if [ "$(id -u)" -ne 0 ]; then
    fail "monitor-agent uninstall: root privileges required"
fi
if [ "$#" -gt 1 ] || { [ "$#" -eq 1 ] && [ "$1" != "--purge" ]; }; then
    fail "monitor-agent uninstall: expected no arguments or --purge"
fi
purge=0
if [ "$#" -eq 1 ]; then
    purge=1
fi

install_root="/Library/Application Support/MonitorAgent"
log_root="/Library/Logs/MonitorAgent"
plist_target="/Library/LaunchDaemons/com.company.monitor-agent.plist"
launchd_label=com.company.monitor-agent
venv_dir="$install_root/venv"
launcher="$install_root/run-agent.sh"
config_file="$install_root/monitor-agent.env"
spool_dir="$install_root/spool"

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
            fail "monitor-agent uninstall: unsafe deployment target"
        fi
        if [ -e "$safe_current" ] && [ ! -d "$safe_current" ]; then
            fail "monitor-agent uninstall: unsafe deployment target"
        fi
        safe_current="$safe_current/"
    done
}

assert_safe_path() {
    path=$1
    if [ -L "$path" ]; then
        fail "monitor-agent uninstall: unsafe deployment target"
    fi
    if [ -e "$path" ] && [ ! -d "$path" ] && [ "$path" != "$plist_target" ] && [ "$path" != "$launcher" ]; then
        fail "monitor-agent uninstall: unsafe deployment target"
    fi
}

read_launchd_state() {
    if launchctl print "system/$launchd_label" >/dev/null 2>&1; then
        launchd_loaded=1
        return 0
    else
        launchd_status=$?
    fi
    if [ "$launchd_status" -eq 113 ]; then
        launchd_loaded=0
        return 0
    fi
    return 1
}

for protected_path in "$install_root" "$log_root" "$(dirname "$plist_target")"; do
    assert_safe_directory_chain "$protected_path"
done
for protected_path in "$install_root" "$log_root" "$venv_dir" "$spool_dir"; do
    assert_safe_path "$protected_path"
done
if [ -L "$plist_target" ] || { [ -e "$plist_target" ] && [ ! -f "$plist_target" ]; }; then
    fail "monitor-agent uninstall: unsafe deployment target"
fi
if [ -L "$launcher" ] || { [ -e "$launcher" ] && [ ! -f "$launcher" ]; }; then
    fail "monitor-agent uninstall: unsafe deployment target"
fi
if [ -L "$config_file" ] || { [ -e "$config_file" ] && [ ! -f "$config_file" ]; }; then
    fail "monitor-agent uninstall: unsafe deployment target"
fi
if ! read_launchd_state; then
    fail "monitor-agent uninstall: unable to inspect LaunchDaemon state"
fi
if [ "$launchd_loaded" -eq 1 ]; then
    launchctl bootout system/com.company.monitor-agent ||
        fail "monitor-agent uninstall: unable to stop LaunchDaemon"
fi
rm -f "$plist_target" || fail "monitor-agent uninstall: unable to remove LaunchDaemon"
if ! read_launchd_state || [ "$launchd_loaded" -ne 0 ]; then
    fail "monitor-agent uninstall: unable to verify LaunchDaemon removal"
fi

if [ -e "$venv_dir" ]; then
    rm -rf "$venv_dir" || fail "monitor-agent uninstall: unable to remove runtime"
fi
if [ -e "$launcher" ]; then
    rm -f "$launcher" || fail "monitor-agent uninstall: unable to remove runtime"
fi
if [ "$purge" -eq 1 ]; then
    if [ -e "$install_root" ]; then
        rm -rf "$install_root" || fail "monitor-agent uninstall: unable to purge installation"
    fi
    if [ -e "$log_root" ]; then
        rm -rf "$log_root" || fail "monitor-agent uninstall: unable to purge logs"
    fi
fi
printf '%s\n' "monitor-agent uninstall: LaunchDaemon removed"
