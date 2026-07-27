#!/usr/bin/env bash
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

systemctl disable --now -- monitor-agent.service >/dev/null 2>&1 || true
rm -rf -- /opt/monitor-agent
rm -f -- /etc/systemd/system/monitor-agent.service

if [ "$purge" -eq 1 ]; then
    rm -rf -- /etc/monitor-agent /var/lib/monitor-agent
fi

systemctl daemon-reload
printf '%s\n' "monitor-agent uninstall: service removed"
