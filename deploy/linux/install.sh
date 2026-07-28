#!/usr/bin/env bash
set -eu

fail() {
    printf '%s\n' "$1" >&2
    exit "${2:-1}"
}

test_mode=${MONITOR_AGENT_TEST_MODE:-}
requested_root=${MONITOR_AGENT_TEST_ROOT:-}

if [ "$EUID" -eq 0 ]; then
    if [ -n "$test_mode" ] || [ -n "$requested_root" ]; then
        fail "monitor-agent install: staging root forbidden for root" 2
    fi
    root_prefix=
    root_anchor=
elif [ "$test_mode" != 1 ]; then
    fail "monitor-agent install: root privileges required" 2
else
    if [ -z "$requested_root" ] ||
        [ "${requested_root#/}" = "$requested_root" ] ||
        [ ! -d "$requested_root" ] ||
        [ -L "$requested_root" ]
    then
        fail "monitor-agent install: invalid staging root"
    fi
    root_prefix=$(realpath -e -- "$requested_root") ||
        fail "monitor-agent install: invalid staging root"
    if [ "$root_prefix" = "/" ] ||
        [ "$(stat -c '%u' -- "$root_prefix")" -ne "$EUID" ]
    then
        fail "monitor-agent install: invalid staging root"
    fi
    exec {root_fd}<"$root_prefix" ||
        fail "monitor-agent install: invalid staging root"
    root_anchor="/proc/$$/fd/$root_fd"
fi

if [ "$#" -ne 2 ]; then
    fail "monitor-agent install: expected WHEEL_PATH ENV_FILE" 2
fi

wheel_path=$1
environment_path=$2
script_dir=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
project_root=$(CDPATH='' cd -- "$script_dir/../.." && pwd)
requirements_path="$project_root/requirements.lock"
service_path="$script_dir/monitor-agent.service"
readme_path="$project_root/README.md"
python_command=${MONITOR_AGENT_PYTHON:-python3.11}

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
if [ "$implementation" != "CPython" ] || [ "$major" != 3 ] || [ -n "${extra:-}" ]; then
    fail "monitor-agent install: unsupported Python interpreter"
fi
case "$minor" in
    11 | 12 | 13 | 14)
        ;;
    *)
        fail "monitor-agent install: unsupported Python interpreter"
        ;;
esac

install_dir="$root_anchor/opt/monitor-agent"
opt_parent="$root_anchor/opt"
etc_parent="$root_anchor/etc"
config_dir="$root_anchor/etc/monitor-agent"
etc_systemd_dir="$root_anchor/etc/systemd"
unit_dir="$root_anchor/etc/systemd/system"
config_file="$config_dir/monitor-agent.env"
var_parent="$root_anchor/var"
var_lib_dir="$root_anchor/var/lib"
state_dir="$root_anchor/var/lib/monitor-agent"
unit_file="$unit_dir/monitor-agent.service"

managed_dirs=(
    "$opt_parent"
    "$etc_parent"
    "$config_dir"
    "$etc_systemd_dir"
    "$unit_dir"
    "$var_parent"
    "$var_lib_dir"
    "$state_dir"
)
managed_modes=(0755 0755 0700 0755 0755 0755 0755 0700)
dir_existed=()
dir_modes=()
dir_fds=()

for index in "${!managed_dirs[@]}"; do
    directory=${managed_dirs[$index]}
    if [ -L "$directory" ]; then
        if [ "$test_mode" = 1 ]; then
            fail "monitor-agent install: invalid staging root"
        fi
        fail "monitor-agent install: invalid install target"
    fi
    if [ -d "$directory" ]; then
        dir_existed[index]=1
        dir_modes[index]=$(stat -c '%a' -- "$directory") ||
            fail "monitor-agent install: unable to inspect install target"
        if [ "$test_mode" = 1 ]; then
            exec {directory_fd}<"$directory" ||
                fail "monitor-agent install: unable to inspect install target"
            dir_fds[index]=$directory_fd
        fi
    elif [ -e "$directory" ]; then
        fail "monitor-agent install: invalid install target"
    else
        dir_existed[index]=0
        dir_modes[index]=
        dir_fds[index]=
    fi
done

if [ -e "$install_dir" ] || [ -L "$install_dir" ]; then
    if [ ! -d "$install_dir" ] || [ -L "$install_dir" ]; then
        fail "monitor-agent install: invalid install target"
    fi
fi
for target in "$config_file" "$unit_file"; do
    if [ -e "$target" ] || [ -L "$target" ]; then
        if [ ! -f "$target" ] || [ -L "$target" ]; then
            fail "monitor-agent install: invalid install target"
        fi
    fi
done

observed_active=0
observed_enabled=0
observed_enabled_absent=0

read_active_state() {
    if service_state=$(systemctl is-active -- monitor-agent.service 2>/dev/null); then
        service_status=0
    else
        service_status=$?
    fi
    case "$service_status:$service_state" in
        0:active)
            observed_active=1
            return 0
            ;;
        3:inactive | 4:not-found)
            observed_active=0
            return 0
            ;;
        *)
            return 1
            ;;
    esac
}

read_enabled_state() {
    if service_state=$(systemctl is-enabled -- monitor-agent.service 2>/dev/null); then
        service_status=0
    else
        service_status=$?
    fi
    case "$service_status:$service_state" in
        0:enabled)
            observed_enabled=1
            observed_enabled_absent=0
            return 0
            ;;
        1:disabled)
            observed_enabled=0
            observed_enabled_absent=0
            return 0
            ;;
        1:not-found | 4:not-found)
            observed_enabled=0
            observed_enabled_absent=1
            return 0
            ;;
        *)
            return 1
            ;;
    esac
}

if ! read_active_state || ! read_enabled_state; then
    fail "monitor-agent install: unable to inspect service state"
fi
if [ "$observed_enabled_absent" -eq 1 ] && [ "$observed_active" -eq 1 ]; then
    fail "monitor-agent install: unable to inspect service state"
fi
prior_active=$observed_active
prior_enabled=$observed_enabled
prior_absent=$observed_enabled_absent

for index in "${!managed_dirs[@]}"; do
    directory=${managed_dirs[$index]}
    if [ "${dir_existed[$index]}" -eq 0 ]; then
        install -d -m "${managed_modes[$index]}" -- "$directory"
    fi
    if [ "$test_mode" = 1 ] && [ -z "${dir_fds[$index]}" ]; then
        exec {directory_fd}<"$directory" ||
            fail "monitor-agent install: unable to inspect install target"
        dir_fds[index]=$directory_fd
    fi
done

managed_cleanup_dirs=("${managed_dirs[@]}")
managed_mode_dirs=("${managed_dirs[@]}")
if [ "$test_mode" = 1 ]; then
    opt_anchor="/proc/$$/fd/${dir_fds[0]}"
    etc_anchor="/proc/$$/fd/${dir_fds[1]}"
    config_anchor="/proc/$$/fd/${dir_fds[2]}"
    etc_systemd_anchor="/proc/$$/fd/${dir_fds[3]}"
    unit_anchor="/proc/$$/fd/${dir_fds[4]}"
    var_anchor="/proc/$$/fd/${dir_fds[5]}"
    var_lib_anchor="/proc/$$/fd/${dir_fds[6]}"
    state_anchor="/proc/$$/fd/${dir_fds[7]}"

    managed_cleanup_dirs=(
        "$root_anchor/opt"
        "$root_anchor/etc"
        "$etc_anchor/monitor-agent"
        "$etc_anchor/systemd"
        "$etc_systemd_anchor/system"
        "$root_anchor/var"
        "$var_anchor/lib"
        "$var_lib_anchor/monitor-agent"
    )
    managed_mode_dirs=(
        "$opt_anchor"
        "$etc_anchor"
        "$config_anchor"
        "$etc_systemd_anchor"
        "$unit_anchor"
        "$var_anchor"
        "$var_lib_anchor"
        "$state_anchor"
    )

    opt_parent=$opt_anchor
    config_dir=$config_anchor
    unit_dir=$unit_anchor
    state_dir=$state_anchor
    install_dir="$opt_anchor/monitor-agent"
    config_file="$config_anchor/monitor-agent.env"
    unit_file="$unit_anchor/monitor-agent.service"
fi

chmod 0700 -- "$config_dir" "$state_dir"

transaction_dir=
staged_runtime=
environment_stage=
environment_backup=
unit_stage=
unit_backup=
had_runtime=0
had_environment=0
had_unit=0
environment_mutation_armed=0
unit_mutation_armed=0
activation_rollback_armed=0
installation_committed=0

restore_service_state() {
    service_restore_failed=0

    if ! systemctl daemon-reload >/dev/null 2>&1; then
        service_restore_failed=1
    fi
    if [ "$prior_absent" -eq 0 ]; then
        if [ "$prior_enabled" -eq 1 ]; then
            if ! systemctl enable -- monitor-agent.service >/dev/null 2>&1; then
                service_restore_failed=1
            fi
        elif ! systemctl disable -- monitor-agent.service >/dev/null 2>&1; then
            service_restore_failed=1
        fi
        if [ "$prior_active" -eq 1 ]; then
            if ! systemctl restart -- monitor-agent.service >/dev/null 2>&1; then
                service_restore_failed=1
            fi
        elif ! systemctl stop -- monitor-agent.service >/dev/null 2>&1; then
            service_restore_failed=1
        fi
    fi

    if ! read_active_state || [ "$observed_active" -ne "$prior_active" ]; then
        service_restore_failed=1
    fi
    if ! read_enabled_state || [ "$observed_enabled" -ne "$prior_enabled" ]; then
        service_restore_failed=1
    fi
    [ "$service_restore_failed" -eq 0 ]
}

cleanup_artifacts() {
    artifact_cleanup_failed=0
    for temporary in \
        "$environment_stage" \
        "$environment_backup" \
        "$unit_stage" \
        "$unit_backup"
    do
        if [ -n "$temporary" ] && ! rm -f -- "$temporary"; then
            artifact_cleanup_failed=1
        fi
    done
    if [ -n "$transaction_dir" ] && ! rm -rf -- "$transaction_dir"; then
        artifact_cleanup_failed=1
    fi
    [ "$artifact_cleanup_failed" -eq 0 ]
}

cleanup() {
    original_status=$?
    trap - EXIT
    set +e
    rollback_failed=0

    if [ "$original_status" -ne 0 ] && [ "$installation_committed" -eq 0 ]; then
        if [ "$environment_mutation_armed" -eq 1 ]; then
            rm -rf -- "$config_file" || rollback_failed=1
            if [ "$had_environment" -eq 1 ] && [ -f "$environment_backup" ]; then
                mv -fT -- "$environment_backup" "$config_file" ||
                    rollback_failed=1
                environment_backup=
            fi
        fi
        if [ "$unit_mutation_armed" -eq 1 ]; then
            rm -rf -- "$unit_file" || rollback_failed=1
            if [ "$had_unit" -eq 1 ] && [ -f "$unit_backup" ]; then
                mv -fT -- "$unit_backup" "$unit_file" || rollback_failed=1
                unit_backup=
            fi
        fi

        if [ "$had_runtime" -eq 1 ] &&
            [ -n "$transaction_dir" ] &&
            [ -d "$transaction_dir/previous-runtime" ]
        then
            rm -rf -- "$install_dir" || rollback_failed=1
            mv -T -- "$transaction_dir/previous-runtime" "$install_dir" ||
                rollback_failed=1
        elif [ "$had_runtime" -eq 0 ] &&
            [ -n "$staged_runtime" ] &&
            [ ! -e "$staged_runtime" ]
        then
            rm -rf -- "$install_dir" || rollback_failed=1
        fi

        if [ "$activation_rollback_armed" -eq 1 ]; then
            restore_service_state || rollback_failed=1
        fi
    fi

    cleanup_artifacts || rollback_failed=1

    if [ "$original_status" -ne 0 ] && [ "$installation_committed" -eq 0 ]; then
        for ((index = ${#managed_cleanup_dirs[@]} - 1; index >= 0; index--)); do
            if [ "${dir_existed[$index]}" -eq 1 ]; then
                chmod "${dir_modes[$index]}" -- "${managed_mode_dirs[$index]}" ||
                    rollback_failed=1
            else
                rm -rf -- "${managed_cleanup_dirs[$index]}" || rollback_failed=1
            fi
        done
        printf '%s\n' "monitor-agent install: installation failed" >&2
        if [ "$rollback_failed" -ne 0 ]; then
            printf '%s\n' "monitor-agent install: rollback failed" >&2
        fi
    fi
    exit "$original_status"
}
trap cleanup EXIT

transaction_dir=$(mktemp -d -- "$opt_parent/.monitor-agent-install.XXXXXX")
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

environment_stage=$(mktemp -- "$config_dir/.monitor-agent.env.stage.XXXXXX")
install -m 0600 -- "$environment_path" "$environment_stage"
unit_stage=$(mktemp -- "$unit_dir/.monitor-agent.service.stage.XXXXXX")
install -m 0644 -- "$service_path" "$unit_stage"

if [ -f "$config_file" ] && [ ! -L "$config_file" ]; then
    had_environment=1
    environment_backup=$(mktemp -- "$config_dir/.monitor-agent.env.backup.XXXXXX")
    cp -p -- "$config_file" "$environment_backup"
fi
if [ -f "$unit_file" ] && [ ! -L "$unit_file" ]; then
    had_unit=1
    unit_backup=$(mktemp -- "$unit_dir/.monitor-agent.service.backup.XXXXXX")
    cp -p -- "$unit_file" "$unit_backup"
fi

if [ -d "$install_dir" ] && [ ! -L "$install_dir" ]; then
    had_runtime=1
    mv -T -- "$install_dir" "$transaction_dir/previous-runtime"
fi
if [ -e "$install_dir" ] || [ -L "$install_dir" ]; then
    fail "monitor-agent install: invalid install target"
fi
mv -nT -- "$staged_runtime" "$install_dir"
if [ -e "$staged_runtime" ] || [ -L "$staged_runtime" ]; then
    fail "monitor-agent install: invalid install target"
fi

environment_mutation_armed=1
if [ -e "$config_file" ] || [ -L "$config_file" ]; then
    if [ ! -f "$config_file" ] || [ -L "$config_file" ]; then
        fail "monitor-agent install: invalid install target"
    fi
fi
mv -fT -- "$environment_stage" "$config_file"
environment_stage=

unit_mutation_armed=1
if [ -e "$unit_file" ] || [ -L "$unit_file" ]; then
    if [ ! -f "$unit_file" ] || [ -L "$unit_file" ]; then
        fail "monitor-agent install: invalid install target"
    fi
fi
mv -fT -- "$unit_stage" "$unit_file"
unit_stage=

activation_rollback_armed=1
systemctl daemon-reload
systemctl enable -- monitor-agent.service
systemctl restart -- monitor-agent.service
if ! read_active_state || [ "$observed_active" -ne 1 ]; then
    fail "monitor-agent install: service failed to become active"
fi

installation_committed=1
if ! cleanup_artifacts; then
    fail "monitor-agent install: cleanup failed"
fi
printf '%s\n' "monitor-agent install: service installed and active"
