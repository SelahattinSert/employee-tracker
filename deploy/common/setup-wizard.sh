# Portable one-click setup wizard, sourced by deploy/<os>/setup.sh.
#
# No secret ships in the package: the collector URL and API token are asked for at install
# time (or read from --token-file), written to an owner-only temporary file, handed to the
# platform install.sh and removed again. Screenshots, active-window titles and file audit
# are sensitive; the wizard asks once whether employees have been notified (default yes) and
# only then writes those collectors and MONITOR_EMPLOYEE_NOTICE_ACK as enabled.
#
# The caller sets: SETUP_SELF, SETUP_DIR, SETUP_INSTALLER, SETUP_ENV_EXAMPLE,
# SETUP_DEFAULT_AUDIT_PATHS, SETUP_CA_DIR, SETUP_OS_NAME.

setup_fail() {
    printf 'SETUP FAILED: %s\n' "$1" >&2
    exit "${2:-1}"
}

setup_require_root() {
    if [ "$(id -u)" -eq 0 ]; then
        return 0
    fi
    if ! command -v sudo >/dev/null 2>&1; then
        setup_fail "run this installer as root (sudo)." 2
    fi
    printf 'Administrator rights are required; re-running with sudo.\n'
    exec sudo -- "$SETUP_SELF" "$@"
}

setup_read_secret() {
    # Reads one hidden line from the terminal into setup_secret.
    printf '%s' "$1" > /dev/tty
    stty -echo < /dev/tty 2>/dev/null || true
    IFS= read -r setup_secret < /dev/tty || setup_secret=
    stty echo < /dev/tty 2>/dev/null || true
    printf '\n' > /dev/tty
}

setup_normalize_uri() {
    value=$1
    case "$value" in
        *://*) ;;
        *) value="https://$value" ;;
    esac
    case "$value" in
        https://*) ;;
        *) setup_fail "the server address must use HTTPS." ;;
    esac
    remainder=${value#https://}
    case "$remainder" in
        *@*) setup_fail "the server address must not contain credentials." ;;
    esac
    case "$remainder" in
        */*) printf '%s' "$value" ;;
        *) printf '%s%s' "$value" "/api/v1/telemetry" ;;
    esac
}

setup_ask_notice() {
    if [ "${setup_notified_opt:-}" = "no" ]; then
        return 1
    fi
    if [ "${setup_notified_opt:-}" = "yes" ]; then
        return 0
    fi
    printf '\n' > /dev/tty
    printf 'Screenshots, active-window titles and file audit are sensitive telemetry.\n' > /dev/tty
    printf 'Collecting them without telling employees may be unlawful. Enable them only\n' > /dev/tty
    printf 'after employees have been notified in line with local law and your policy.\n' > /dev/tty
    printf 'Have employees been notified? Enable these collectors? [Y/n] ' > /dev/tty
    IFS= read -r setup_answer < /dev/tty || setup_answer=
    case "$setup_answer" in
        [nN]*) return 1 ;;
        *) return 0 ;;
    esac
}

setup_write_config() {
    output=$1
    : > "$output"
    while IFS= read -r line || [ -n "$line" ]; do
        key=${line%%=*}
        case "$key" in
            MONITOR_COLLECTOR_URI)
                printf 'MONITOR_COLLECTOR_URI=%s\n' "$setup_collector_uri" >> "$output" ;;
            MONITOR_API_TOKEN)
                printf 'MONITOR_API_TOKEN=%s\n' "$setup_token" >> "$output" ;;
            MONITOR_CA_BUNDLE)
                : ;;
            MONITOR_SCREENSHOT_ENABLED)
                if [ "$setup_notified" -eq 1 ]; then
                    printf 'MONITOR_SCREENSHOT_ENABLED=true\n' >> "$output"
                else
                    printf '%s\n' "$line" >> "$output"
                fi ;;
            MONITOR_INCLUDE_ACTIVE_WINDOW)
                if [ "$setup_notified" -eq 1 ]; then
                    printf 'MONITOR_INCLUDE_ACTIVE_WINDOW=true\n' >> "$output"
                else
                    printf '%s\n' "$line" >> "$output"
                fi ;;
            MONITOR_AUDIT_PATHS)
                if [ "$setup_notified" -eq 1 ]; then
                    printf 'MONITOR_AUDIT_PATHS=%s\n' "$setup_audit_paths" >> "$output"
                else
                    printf '%s\n' "$line" >> "$output"
                fi ;;
            MONITOR_EMPLOYEE_NOTICE_ACK)
                if [ "$setup_notified" -eq 1 ]; then
                    printf 'MONITOR_EMPLOYEE_NOTICE_ACK=true\n' >> "$output"
                else
                    printf '%s\n' "$line" >> "$output"
                fi ;;
            *)
                printf '%s\n' "$line" >> "$output" ;;
        esac
    done < "$SETUP_ENV_EXAMPLE"
    if [ -n "$setup_ca_bundle" ]; then
        printf 'MONITOR_CA_BUNDLE=%s\n' "$setup_ca_bundle" >> "$output"
    fi
}

setup_find_python() {
    if [ -n "${setup_python_opt:-}" ]; then
        if command -v "$setup_python_opt" >/dev/null 2>&1; then
            printf '%s' "$setup_python_opt"
            return 0
        fi
        return 1
    fi
    for candidate in python3.14 python3.13 python3.12 python3.11; do
        if command -v "$candidate" >/dev/null 2>&1; then
            printf '%s' "$candidate"
            return 0
        fi
    done
    return 1
}

setup_main() {
    setup_require_root "$@"

    server_url=
    setup_token_file=
    setup_ca_file=
    setup_notified_opt=
    setup_audit_opt=
    setup_python_opt=
    while [ "$#" -gt 0 ]; do
        case "$1" in
            --server-url) server_url=$2; shift 2 ;;
            --server-url=*) server_url=${1#*=}; shift ;;
            --token-file) setup_token_file=$2; shift 2 ;;
            --token-file=*) setup_token_file=${1#*=}; shift ;;
            --ca-file) setup_ca_file=$2; shift 2 ;;
            --ca-file=*) setup_ca_file=${1#*=}; shift ;;
            --employees-notified) setup_notified_opt=yes; shift ;;
            --no-employees-notified) setup_notified_opt=no; shift ;;
            --audit-paths) setup_audit_opt=$2; shift 2 ;;
            --audit-paths=*) setup_audit_opt=${1#*=}; shift ;;
            --python) setup_python_opt=$2; shift 2 ;;
            --python=*) setup_python_opt=${1#*=}; shift ;;
            *) setup_fail "unknown option: $1" 2 ;;
        esac
    done

    printf '== Monitor Agent setup (%s) ==\n' "$SETUP_OS_NAME"

    if [ ! -f "$SETUP_INSTALLER" ]; then
        setup_fail "the platform installer was not found next to this script."
    fi
    package_root=$(CDPATH='' cd -- "$SETUP_DIR/../.." && pwd) ||
        setup_fail "could not resolve the package root."
    setup_wheel=
    for candidate in "$package_root"/dist/monitor_agent-*.whl; do
        if [ -f "$candidate" ]; then
            setup_wheel=$candidate
        fi
    done
    [ -n "$setup_wheel" ] || setup_fail "no monitor_agent wheel was found in the dist folder."

    if [ -z "$server_url" ]; then
        printf 'Server address (for example https://monitor.example.com:8443): ' > /dev/tty
        IFS= read -r server_url < /dev/tty || server_url=
    fi
    [ -n "$server_url" ] || setup_fail "a server address is required."
    setup_collector_uri=$(setup_normalize_uri "$server_url")
    printf 'Collector URL: %s\n' "$setup_collector_uri"

    if [ -n "$setup_token_file" ]; then
        [ -f "$setup_token_file" ] || setup_fail "token file not found."
        setup_token=$(tr -d '\r\n' < "$setup_token_file")
    else
        setup_read_secret "Agent API token (input is hidden): "
        setup_token=$setup_secret
        setup_secret=
    fi
    case "$setup_token" in
        '' | *[!!-~]*) setup_fail "the API token is empty or contains whitespace." ;;
    esac

    if [ -z "$setup_ca_file" ] && [ -f "$package_root/ca.crt" ]; then
        setup_ca_file="$package_root/ca.crt"
    fi
    setup_ca_bundle=
    if [ -n "$setup_ca_file" ]; then
        [ -f "$setup_ca_file" ] || setup_fail "CA file not found."
        mkdir -p "$SETUP_CA_DIR"
        chmod 0755 "$SETUP_CA_DIR" 2>/dev/null || true
        cp "$setup_ca_file" "$SETUP_CA_DIR/ca.crt"
        chmod 0644 "$SETUP_CA_DIR/ca.crt"
        setup_ca_bundle="$SETUP_CA_DIR/ca.crt"
        printf 'Private CA installed at %s\n' "$setup_ca_bundle"
    fi

    setup_python=$(setup_find_python) ||
        setup_fail "install Python 3.11-3.14, then run the installer again."
    printf 'Using %s\n' "$setup_python"

    if setup_ask_notice; then
        setup_notified=1
        if [ -n "$setup_audit_opt" ]; then
            setup_audit_paths=$setup_audit_opt
        else
            setup_audit_paths=$SETUP_DEFAULT_AUDIT_PATHS
        fi
        printf 'Sensitive collection enabled: screenshots, active window, file audit (%s).\n' \
            "$setup_audit_paths"
    else
        setup_notified=0
        setup_audit_paths=
        printf 'Sensitive collection left disabled (no employee notice acknowledged).\n'
    fi

    setup_umask_old=$(umask)
    umask 077
    setup_workdir=$(mktemp -d) || setup_fail "could not create a temporary folder."
    umask "$setup_umask_old"
    trap 'rm -rf "$setup_workdir"' EXIT INT TERM
    setup_config="$setup_workdir/monitor-agent.env"
    setup_write_config "$setup_config"
    setup_token=

    MONITOR_AGENT_PYTHON="$setup_python" "$SETUP_INSTALLER" "$setup_wheel" "$setup_config" ||
        setup_fail "the platform installer failed."

    rm -rf "$setup_workdir"
    trap - EXIT INT TERM
    printf '\nMonitor Agent is installed. The machine appears on the server after its first event.\n'
}
