#!/bin/sh
# One-click Monitor Agent setup for macOS. Wraps deploy/macos/install.sh: asks for the
# server address and API token, asks once whether employees have been notified (default
# yes) before enabling sensitive collectors, writes an owner-only temporary configuration
# and runs the installer. Unattended: --server-url, --token-file, --ca-file,
# --employees-notified / --no-employees-notified, --audit-paths, --python.
set -eu

SETUP_SELF=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)/$(basename -- "$0")
SETUP_DIR=$(dirname -- "$SETUP_SELF")
SETUP_INSTALLER="$SETUP_DIR/install.sh"
SETUP_ENV_EXAMPLE="$SETUP_DIR/monitor-agent.env.example"
SETUP_DEFAULT_AUDIT_PATHS="/Users"
SETUP_CA_DIR="/Library/Application Support/MonitorAgentCA"
SETUP_OS_NAME="macOS"
export SETUP_SELF SETUP_DIR SETUP_INSTALLER SETUP_ENV_EXAMPLE
export SETUP_DEFAULT_AUDIT_PATHS SETUP_CA_DIR SETUP_OS_NAME

# shellcheck source=deploy/common/setup-wizard.sh
. "$SETUP_DIR/../common/setup-wizard.sh"
setup_main "$@"
