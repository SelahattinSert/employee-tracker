#!/usr/bin/env bash
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
script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
project_root=$(CDPATH= cd -- "$script_dir/../.." && pwd)
requirements_path="$project_root/requirements.lock"
service_path="$script_dir/monitor-agent.service"
readme_path="$project_root/README.md"
python_command=${MONITOR_AGENT_PYTHON:-python3.11}
root_prefix=${MONITOR_AGENT_TEST_ROOT:-}

case "$root_prefix" in
    "")
        ;;
    /*)
        if [ "$root_prefix" = "/" ]; then
            fail "monitor-agent install: invalid staging root"
        fi
        ;;
    *)
        fail "monitor-agent install: invalid staging root"
        ;;
esac

case "$wheel_path" in
    *.whl)
        if [ ! -f "$wheel_path" ] || [ -L "$wheel_path" ]; then
            fail "monitor-agent install: invalid wheel"
        fi
        ;;
    *)
        fail "monitor-agent install: invalid wheel"
        ;;
esac

if [ ! -f "$environment_path" ] || [ -L "$environment_path" ]; then
    fail "monitor-agent install: invalid environment file"
fi
if [ ! -f "$requirements_path" ] || [ -L "$requirements_path" ]; then
    fail "monitor-agent install: invalid requirements lock"
fi
if [ ! -f "$service_path" ] || [ -L "$service_path" ]; then
    fail "monitor-agent install: invalid service file"
fi
if ! command -v "$python_command" >/dev/null 2>&1; then
    fail "monitor-agent install: Python interpreter not found"
fi
if ! command -v systemctl >/dev/null 2>&1; then
    fail "monitor-agent install: systemctl not found"
fi

version_output=$(
    "$python_command" -c \
        'import platform, sys; print(platform.python_implementation(), *sys.version_info[:2])'
) || fail "monitor-agent install: unable to inspect Python interpreter"
IFS=' ' read -r implementation major minor extra <<< "$version_output"
if [ "$implementation" != "CPython" ] || [ "$major" != "3" ] || [ -n "${extra:-}" ]; then
    fail "monitor-agent install: unsupported Python interpreter"
fi
case "$minor" in
    11 | 12 | 13 | 14)
        ;;
    *)
        fail "monitor-agent install: unsupported Python interpreter"
        ;;
esac

install_dir="$root_prefix/opt/monitor-agent"
opt_parent="$root_prefix/opt"
config_dir="$root_prefix/etc/monitor-agent"
config_file="$config_dir/monitor-agent.env"
state_dir="$root_prefix/var/lib/monitor-agent"
unit_dir="$root_prefix/etc/systemd/system"
unit_file="$unit_dir/monitor-agent.service"

transaction_dir=
runtime_detached=0
runtime_installed=0
environment_installed=0
unit_installed=0
activation_attempted=0
had_runtime=0
had_environment=0
had_unit=0

cleanup() {
    status=$?
    trap - EXIT

    if [ "$status" -ne 0 ] && [ -n "$transaction_dir" ]; then
        if [ "$runtime_installed" -eq 1 ]; then
            rm -rf -- "$install_dir"
        fi
        if [ "$runtime_detached" -eq 1 ]; then
            mv -- "$transaction_dir/previous-runtime" "$install_dir"
        fi

        if [ "$environment_installed" -eq 1 ]; then
            if [ "$had_environment" -eq 1 ]; then
                cp -p -- "$transaction_dir/previous-environment" "$config_file"
            else
                rm -f -- "$config_file"
            fi
        fi
        if [ "$unit_installed" -eq 1 ]; then
            if [ "$had_unit" -eq 1 ]; then
                cp -p -- "$transaction_dir/previous-unit" "$unit_file"
            else
                rm -f -- "$unit_file"
            fi
        fi

        if [ "$unit_installed" -eq 1 ]; then
            systemctl daemon-reload >/dev/null 2>&1 || true
        fi
        if [ "$activation_attempted" -eq 1 ] && [ "$had_runtime" -eq 1 ]; then
            systemctl restart -- monitor-agent.service >/dev/null 2>&1 || true
        fi
        printf '%s\n' "monitor-agent install: installation failed" >&2
    fi

    if [ -n "$transaction_dir" ]; then
        rm -rf -- "$transaction_dir"
    fi
    exit "$status"
}
trap cleanup EXIT

install -d -m 0755 -- "$opt_parent"
transaction_dir=$(mktemp -d "$opt_parent/.monitor-agent-install.XXXXXX")
staged_runtime="$transaction_dir/runtime"
install -d -m 0755 -- "$staged_runtime"

"$python_command" -m venv "$staged_runtime/venv"
"$staged_runtime/venv/bin/python" -m pip install \
    --require-hashes -r "$requirements_path"
"$staged_runtime/venv/bin/python" -m pip install \
    --no-deps --force-reinstall -- "$wheel_path"
if [ -f "$readme_path" ]; then
    install -m 0644 -- "$readme_path" "$staged_runtime/README.md"
fi

install -d -m 0700 -- "$config_dir"
install -d -m 0700 -- "$state_dir"
install -d -m 0755 -- "$unit_dir"

if [ -e "$install_dir" ]; then
    had_runtime=1
    mv -- "$install_dir" "$transaction_dir/previous-runtime"
    runtime_detached=1
fi
if [ -f "$config_file" ]; then
    had_environment=1
    cp -p -- "$config_file" "$transaction_dir/previous-environment"
fi
if [ -f "$unit_file" ]; then
    had_unit=1
    cp -p -- "$unit_file" "$transaction_dir/previous-unit"
fi

mv -- "$staged_runtime" "$install_dir"
runtime_installed=1
install -m 0600 -- "$environment_path" "$config_file"
environment_installed=1
install -m 0644 -- "$service_path" "$unit_file"
unit_installed=1

systemctl daemon-reload
systemctl enable -- monitor-agent.service
activation_attempted=1
systemctl restart -- monitor-agent.service
systemctl is-active --quiet -- monitor-agent.service

printf '%s\n' "monitor-agent install: service installed and active"
