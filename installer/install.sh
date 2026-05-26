#!/bin/sh
# probe tmp-launcher.
#
# Usage from the friend's terminal (operator sends this one-liner via DM):
#   curl -fsSL https://raw.githubusercontent.com/phy0x1a79ed/awm-probe/main/installer/install.sh \
#       | EMQX_PASS=<password> sh -s <probe-name>
#
# EMQX URL + USER are baked into the binary. Only the rotatable password
# travels in the curl invocation (or pre-set in the environment).
set -e

ARCH="$(uname -m)"
case "$ARCH" in
    x86_64|amd64) ASSET="probe-linux-x86_64" ;;
    *) printf 'probe: unsupported arch %s (v1 is linux-x86_64 only)\n' "$ARCH" >&2; exit 1 ;;
esac

NAME="${1:-}"
if [ -z "$NAME" ]; then
    printf 'probe: missing required name argument\n' >&2
    printf 'usage: curl -fsSL <url> | EMQX_PASS=<pass> sh -s <probe-name>\n' >&2
    exit 2
fi

if [ -z "${EMQX_PASS:-}" ]; then
    printf 'probe: EMQX_PASS env var required\n' >&2
    printf 'usage: curl -fsSL <url> | EMQX_PASS=<pass> sh -s <probe-name>\n' >&2
    exit 3
fi

# Allow override (host_binary.py local delivery, or pinning a release tag).
BINARY_URL="${PROBE_BINARY_URL:-https://github.com/phy0x1a79ed/awm-probe/releases/latest/download/${ASSET}}"

DEST="${TMPDIR:-/tmp}/probe-$(id -u)"
curl -fsSL "$BINARY_URL" -o "$DEST"
chmod +x "$DEST"
# Re-open stdin from the controlling tty so the consent prompt works
# under `curl … | sh -s <name>` (curl owns the pipe-stdin; without
# /dev/tty the binary would read EOF and bail).
EMQX_PASS="$EMQX_PASS" exec "$DEST" --name "$NAME" < /dev/tty
