#!/usr/bin/env bash
# Build the trilium plugin's own runtime: the conda env awm-trilium, or AWM_ENV, and
# unpack the Trilium server release that server.pin names (install.md R115 item 4).
#
# The release is a tarball that the fork (projects/trilium on mizar) built, with
# its own Node runtime. awm's deploy pushes it into this node's artifact store
# (<AWM_DIR>/artifacts); this script never builds the fork and never downloads.
# A node the deploy pushed no tarball to installs the service without a server.
# Every step is idempotent, because this script runs on every deploy.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ENV="${AWM_ENV:-awm-trilium}"

# Two different roots, and they coincide only in the deployed tree. REPO is the
# awm checkout this script belongs to. WS is the workspace holding `projects/`
# and `.awm/`. Run from a scope worktree they differ, and using the git toplevel
# for a `projects/` path silently addresses a directory that does not exist.
REPO="$(git -C "$HERE" rev-parse --show-toplevel)"
AWM="${AWM_ROOT:-$REPO/awm}"
workspace_root() {
    [ -n "${AWM_WORKSPACE:-}" ] && { printf '%s\n' "$AWM_WORKSPACE"; return; }
    local d="$1"
    while [ "$d" != "/" ]; do
        if [ -d "$d/projects" ] && [ -f "$d/AGENTS.md" ]; then
            printf '%s\n' "$d"
            return
        fi
        d="$(dirname "$d")"
    done
    printf '%s\n' "$REPO"          # standalone checkout: nothing above it
}
WS="$(workspace_root "$HERE")"

# Runtime state (the logs) vs install artifacts (the unpacked server). They are
# separate because the user that installs and the user that runs need not be
# the same one — see awm/trilium/instances.py.
STATE_DIR="${TRILIUM_STATE_DIR:-$WS/.awm/services/trilium}"
INSTALL_DIR="$HERE"
SERVER_DIR="$INSTALL_DIR/server"
STAMP_FILE="$INSTALL_DIR/tarball-stamp"
ARTIFACTS="${AWM_DIR:-$WS/.awm}/artifacts"

pin() { sed -n "s/^$1=//p" "$HERE/server.pin" | head -1; }
PIN_VERSION="$(pin version)"; PIN_FILE="$(pin file)"; PIN_SHA="$(pin sha256)"
[ -n "$PIN_VERSION" ] && [ -n "$PIN_FILE" ] && [ -n "$PIN_SHA" ] || {
    echo "error: server.pin needs version, file and sha256." >&2; exit 1; }

. "$AWM/plugin-env.sh"

run "$AWM/service_components/config" --no-deps
run "$AWM/service_components/persistence" --no-deps
run "$AWM/service_components/gatewayclient" --no-deps
run "$HERE"

# Written before the server unpack, whose no-tarball path exits early.
bake_runtime_env

# Best effort: where the state root belongs to another account, the service
# creates what it needs at runtime, as that account.
mkdir -p "$STATE_DIR" 2>/dev/null || true

WANT_STAMP="$PIN_VERSION sha256:$PIN_SHA"
if [ "$(cat "$STAMP_FILE" 2>/dev/null || true)" = "$WANT_STAMP" ] \
   && [ -f "$SERVER_DIR/main.cjs" ]; then
    echo "Trilium fork release $PIN_VERSION already unpacked — nothing to do."
elif [ ! -f "$ARTIFACTS/$PIN_FILE" ]; then
    echo "warning: no $PIN_FILE in $ARTIFACTS — trilium installs without a server." >&2
    echo "  awm's deploy pushes it to the nodes that serve Trilium." >&2
    if [ "${TRILIUM_REQUIRE_SERVER:-}" = "1" ]; then
        exit 1
    fi
    exit 0
else
    GOT="$(sha256sum "$ARTIFACTS/$PIN_FILE" | cut -d' ' -f1)"
    [ "$GOT" = "$PIN_SHA" ] || {
        echo "error: $ARTIFACTS/$PIN_FILE has sha256 $GOT, and server.pin names $PIN_SHA." >&2
        exit 1; }
    rm -rf "$SERVER_DIR.incoming"
    mkdir -p "$SERVER_DIR.incoming"
    tar -xJf "$ARTIFACTS/$PIN_FILE" -C "$SERVER_DIR.incoming" --strip-components=1
    test -f "$SERVER_DIR.incoming/main.cjs" && test -x "$SERVER_DIR.incoming/node/bin/node" || {
        echo "error: $PIN_FILE holds no main.cjs and node/bin/node at its root." >&2
        rm -rf "$SERVER_DIR.incoming"; exit 1; }
    rm -rf "$SERVER_DIR"
    mv "$SERVER_DIR.incoming" "$SERVER_DIR"
    printf '%s\n' "$WANT_STAMP" > "$STAMP_FILE"
fi

echo "Installed awm-trilium into env '$ENV'."
echo "Server:  $SERVER_DIR/main.cjs (fork release $PIN_VERSION)"
echo "State:   $STATE_DIR"
