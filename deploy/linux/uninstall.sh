#!/usr/bin/env bash
set -eu

fail() {
    printf '%s\n' "$1" >&2
    exit 2
}

if [ "$EUID" -ne 0 ]; then
    fail "monitor-agent uninstall: root privileges required"
fi

if [ "$#" -gt 1 ] || { [ "$#" -eq 1 ] && [ "$1" != "--purge" ]; }; then
    fail "monitor-agent uninstall: expected no arguments or --purge"
fi

purge=0
if [ "$#" -eq 1 ]; then
    purge=1
fi

systemctl disable --now -- monitor-agent.service >/dev/null 2>&1 || true

if active_state=$(systemctl is-active -- monitor-agent.service 2>/dev/null); then
    active_status=0
else
    active_status=$?
fi
case "$active_status:$active_state" in
    0:active)
        fail "monitor-agent uninstall: service is still active"
        ;;
    3:inactive | 4:not-found)
        ;;
    *)
        fail "monitor-agent uninstall: unable to verify service inactive"
        ;;
esac

if enabled_state=$(systemctl is-enabled -- monitor-agent.service 2>/dev/null); then
    enabled_status=0
else
    enabled_status=$?
fi
case "$enabled_status:$enabled_state" in
    0:enabled)
        fail "monitor-agent uninstall: service is still enabled"
        ;;
    1:disabled | 1:not-found | 4:not-found)
        ;;
    *)
        fail "monitor-agent uninstall: unable to verify service disabled"
        ;;
esac

rm -rf -- /opt/monitor-agent
rm -f -- /etc/systemd/system/monitor-agent.service

if [ "$purge" -eq 1 ]; then
    rm -rf -- /etc/monitor-agent /var/lib/monitor-agent
fi

systemctl daemon-reload
printf '%s\n' "monitor-agent uninstall: service removed"
