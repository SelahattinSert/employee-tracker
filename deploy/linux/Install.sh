#!/bin/sh
# Double-clickable / run-as-program launcher: runs the Linux setup wizard, which asks for
# administrator rights, the server address and the API token.
set -eu
here=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
exec sh "$here/deploy/linux/setup.sh" "$@"
