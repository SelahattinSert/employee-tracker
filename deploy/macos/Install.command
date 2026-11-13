#!/bin/sh
# Double-click in Finder to run the macOS setup wizard, which asks for administrator rights,
# the server address and the API token.
set -eu
here=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
exec sh "$here/deploy/macos/setup.sh" "$@"
