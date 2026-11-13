#!/bin/sh
# Wrap a tar.gz archive into a single self-extracting POSIX shell installer (.run).
#
# Usage: make_selfextract.sh ARCHIVE.tar.gz LAUNCHER OUTPUT.run
#   ARCHIVE   a gzip tarball whose single top-level directory holds LAUNCHER.
#   LAUNCHER  path of the launcher inside that directory, e.g. Install.sh.
#   OUTPUT    the self-extracting script to write.
#
# Running OUTPUT extracts the payload to a private temporary directory and executes the
# launcher. No secret is embedded; the launcher asks for the server address and token.
set -eu

if [ "$#" -ne 3 ]; then
    printf 'usage: make_selfextract.sh ARCHIVE.tar.gz LAUNCHER OUTPUT.run\n' >&2
    exit 2
fi

archive=$1
launcher=$2
output=$3

[ -f "$archive" ] || { printf 'archive not found: %s\n' "$archive" >&2; exit 1; }
case "$launcher" in
    */* | '' | *' '*) printf 'invalid launcher name: %s\n' "$launcher" >&2; exit 1 ;;
esac

tmp_output="$output.tmp.$$"
trap 'rm -f "$tmp_output"' EXIT INT TERM

{
    cat <<STUB
#!/bin/sh
# Self-extracting Monitor Agent installer. Runs the setup wizard, which asks for the
# server address, API token and the employee-notice confirmation.
set -eu
__MONITOR_LAUNCHER__="$launcher"
STUB
    cat <<'STUB'
work=$(mktemp -d) || { echo "cannot create a temporary directory" >&2; exit 1; }
trap 'rm -rf "$work"' EXIT INT TERM
offset=$(awk '/^__MONITOR_PAYLOAD_BELOW__$/ { print NR + 1; exit }' "$0")
if [ -z "$offset" ]; then echo "payload marker missing" >&2; exit 1; fi
tail -n +"$offset" "$0" | tar -xzf - -C "$work"
root=$(find "$work" -mindepth 1 -maxdepth 1 -type d | head -n 1)
if [ -z "$root" ] || [ ! -f "$root/$__MONITOR_LAUNCHER__" ]; then
    echo "installer payload is incomplete" >&2
    exit 1
fi
sh "$root/$__MONITOR_LAUNCHER__" "$@"
status=$?
rm -rf "$work"
trap - EXIT INT TERM
exit $status
__MONITOR_PAYLOAD_BELOW__
STUB
    cat "$archive"
} > "$tmp_output"

chmod 0755 "$tmp_output"
mv "$tmp_output" "$output"
trap - EXIT INT TERM
printf '%s\n' "$output"
