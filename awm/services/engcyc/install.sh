#!/usr/bin/env bash
# Canonical install for the awm-engcyc service (editable, into the `awm` env),
# plus the portal's own `engcyc` env when this node holds a portal checkout.
#
# A node without a checkout is a node that does not serve the portal: the
# service installs, registers idle, and says why in `status`. Exiting non-zero
# there would abort the whole fleet deploy, which runs every service's install
# under `set -e`. ENGCYC_REQUIRE_PORTAL=1 is for the node that must serve it.
#
# Override the target env with AWM_ENV=<name>, the portal env with ENGCYC_ENV.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ENV="${AWM_ENV:-awm}"
PORTAL_ENV="${ENGCYC_ENV:-engcyc}"

# REPO is the awm checkout; WS is the workspace holding `projects/` and `.awm/`.
# They coincide only in the deployed tree.
REPO="$(git -C "$HERE" rev-parse --show-toplevel)"
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
    printf '%s\n' "$REPO"
}
WS="$(workspace_root "$HERE")"
STATE_DIR="${ENGCYC_STATE:-$WS/.awm/services/engcyc}"
PORTAL_DIR="${ENGCYC_PORTAL_DIR:-$WS/projects/engcyc-portal/release}"

run() { echo "+ pip install -e $*"; mamba run -n "$ENV" pip install -e "$@"; }

run "$REPO/awm/service_components/config" --no-deps
run "$REPO/awm/service_components/gatewayclient" --no-deps
run "$REPO/awm/services/engcyc"

PYBIN="$(mamba run -n "$ENV" python -c 'import sys; print(sys.executable)')"
printf 'AWM_PYTHON=%s\nAWM_ENV_BIN=%s\n' "$PYBIN" "$(dirname "$PYBIN")" \
    > "$HERE/.runtime-env"

if [ ! -f "$PORTAL_DIR/src/server/wsgi.py" ]; then
    echo "No portal checkout at $PORTAL_DIR — engcyc will register idle." >&2
    [ "${ENGCYC_REQUIRE_PORTAL:-}" = "1" ] && exit 1
    echo "Installed awm-engcyc into env '$ENV' (no portal)."
    exit 0
fi

# The portal env tracks its environment.yml. Rebuilding is slow, so it is
# stamped with the file's hash and skipped while the file has not moved.
mkdir -p "$STATE_DIR"
STAMP="$STATE_DIR/env-stamp"
WANT="$PORTAL_ENV $(sha256sum "$PORTAL_DIR/environment.yml" | cut -d' ' -f1)"
if [ "$(cat "$STAMP" 2>/dev/null || true)" = "$WANT" ] \
   && mamba run -n "$PORTAL_ENV" gunicorn --version >/dev/null 2>&1; then
    echo "Portal env '$PORTAL_ENV' matches environment.yml — nothing to do."
elif mamba env list | awk '{print $1}' | grep -qx "$PORTAL_ENV"; then
    mamba env update -n "$PORTAL_ENV" -f "$PORTAL_DIR/environment.yml" --prune
    printf '%s\n' "$WANT" > "$STAMP"
else
    mamba env create -n "$PORTAL_ENV" -f "$PORTAL_DIR/environment.yml"
    printf '%s\n' "$WANT" > "$STAMP"
fi

# The supervisor respawns under systemd's minimal PATH, where `mamba` does not
# exist, so it launches gunicorn by the absolute path recorded here.
PORTAL_PY="$(mamba run -n "$PORTAL_ENV" python -c 'import sys; print(sys.executable)')"
printf '%s\n' "$(dirname "$PORTAL_PY")/gunicorn" > "$STATE_DIR/gunicorn-bin"

echo "Installed awm-engcyc into env '$ENV'."
echo "Portal: $PORTAL_DIR ($(git -C "$PORTAL_DIR" describe --always --dirty 2>/dev/null || echo unversioned))"
echo "State:  $STATE_DIR"
