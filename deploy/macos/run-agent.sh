#!/bin/sh
set -eu

fail() {
    printf '%s\n' "$1" >&2
    exit 2
}

script_dir=$(CDPATH=''; export CDPATH; cd -P "$(dirname "$0")" && pwd -P) ||
    fail "monitor-agent launcher: unavailable"
install_root=$script_dir
config_file="$install_root/monitor-agent.env"
agent="$install_root/venv/bin/monitor-agent"
command=run

if [ "$#" -gt 1 ]; then
    fail "monitor-agent launcher: expected run, check-config, or health"
fi
if [ "$#" -eq 1 ]; then
    command=$1
fi
case "$command" in
    run | check-config | health) ;;
    *) fail "monitor-agent launcher: expected run, check-config, or health" ;;
esac

if [ ! -f "$config_file" ] || [ -L "$config_file" ] ||
    [ ! -f "$agent" ] || [ -L "$agent" ]; then
    fail "monitor-agent launcher: unavailable"
fi

config_owner=$(stat -f '%u' "$config_file" 2>/dev/null) ||
    fail "monitor-agent launcher: unavailable"
config_mode=$(stat -f '%Lp' "$config_file" 2>/dev/null) ||
    fail "monitor-agent launcher: unavailable"
if [ "$config_owner" != 0 ]; then
    fail "monitor-agent launcher: unavailable"
fi
case "$config_mode" in
    [0-7][0-7][0-7]) ;;
    *) fail "monitor-agent launcher: unavailable" ;;
esac
case "$config_mode" in
    ?[1-7][0-7] | ??[1-7]) fail "monitor-agent launcher: unavailable" ;;
esac

known_keys='MONITOR_COLLECTOR_URI MONITOR_API_TOKEN MONITOR_CA_BUNDLE MONITOR_HEARTBEAT_SEC MONITOR_STARTUP_DELAY_SEC MONITOR_CONNECT_TIMEOUT_SEC MONITOR_READ_TIMEOUT_SEC MONITOR_COLLECTION_TIMEOUT_SEC MONITOR_MAX_COLLECTOR_WORKERS MONITOR_SPOOL_PATH MONITOR_SPOOL_MAX_BYTES MONITOR_SPOOL_MAX_AGE_SEC MONITOR_REPLAY_BATCH_SIZE MONITOR_PROCESS_CMDLINE_MODE MONITOR_INCLUDE_NETWORK_CONNECTIONS MONITOR_INCLUDE_SOFTWARE MONITOR_LOG_PATH MONITOR_LOG_FORMAT MONITOR_LOG_LEVEL'
for known_key in $known_keys; do
    unset "$known_key"
done

seen_keys=
cr=$(printf '\r')
while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in
        *"$cr") line=${line%"$cr"} ;;
    esac
    trimmed=$line
    while :; do
        case "$trimmed" in
            [[:space:]]*) trimmed=${trimmed#?} ;;
            *) break ;;
        esac
    done
    case "$trimmed" in
        '' | \#*) continue ;;
    esac
    case "$line" in
        *=*)
            key=${line%%=*}
            value=${line#*=}
            ;;
        *) fail "monitor-agent launcher: invalid protected configuration" ;;
    esac
    case "$key" in
        [A-Z][A-Z0-9_]*) ;;
        *) fail "monitor-agent launcher: invalid protected configuration" ;;
    esac
    case "$key" in
        MONITOR_COLLECTOR_URI | MONITOR_API_TOKEN | MONITOR_CA_BUNDLE | \
        MONITOR_HEARTBEAT_SEC | MONITOR_STARTUP_DELAY_SEC | \
        MONITOR_CONNECT_TIMEOUT_SEC | MONITOR_READ_TIMEOUT_SEC | \
        MONITOR_COLLECTION_TIMEOUT_SEC | MONITOR_MAX_COLLECTOR_WORKERS | \
        MONITOR_SPOOL_PATH | MONITOR_SPOOL_MAX_BYTES | \
        MONITOR_SPOOL_MAX_AGE_SEC | MONITOR_REPLAY_BATCH_SIZE | \
        MONITOR_PROCESS_CMDLINE_MODE | MONITOR_INCLUDE_NETWORK_CONNECTIONS | \
        MONITOR_INCLUDE_SOFTWARE | MONITOR_LOG_PATH | MONITOR_LOG_FORMAT | \
        MONITOR_LOG_LEVEL) ;;
        *) fail "monitor-agent launcher: invalid protected configuration" ;;
    esac
    case " $seen_keys " in
        *" $key "*) fail "monitor-agent launcher: invalid protected configuration" ;;
    esac
    seen_keys="$seen_keys $key"
    export "$key=$value"
done < "$config_file"

case " $seen_keys " in
    *" MONITOR_SPOOL_PATH "*)
        if [ "$MONITOR_SPOOL_PATH" != "/Library/Application Support/MonitorAgent/spool" ]; then
            fail "monitor-agent launcher: invalid protected configuration"
        fi
        ;;
esac
case " $seen_keys " in
    *" MONITOR_LOG_PATH "*)
        if [ "$MONITOR_LOG_PATH" != "/Library/Logs/MonitorAgent/monitor-agent.log" ]; then
            fail "monitor-agent launcher: invalid protected configuration"
        fi
        ;;
esac

# This directory exists only in the installer's private staging tree. It keeps
# a real check-config invocation from touching live paths before publication.
if [ "$command" = check-config ] &&
    [ "$install_root" != "/Library/Application Support/MonitorAgent" ] &&
    [ -d "$install_root/.path-validation" ] &&
    [ ! -L "$install_root/.path-validation" ]; then
    export "MONITOR_SPOOL_PATH=$install_root/.path-validation/spool"
    export "MONITOR_LOG_PATH=$install_root/.path-validation/logs/monitor-agent.log"
fi

exec "$agent" "$command"
