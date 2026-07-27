#!/usr/bin/env bash
# Canonical install for the awm-claude-view service (editable, into the `awm` env).
#
# Two halves, because this service has two kinds of dependency:
#
#   1. Python — the adapter, the supervisor, and the HTTPS front. Installs the
#      component libraries it imports first, then the service. Note the
#      httpsfront dependency: this service reuses that front's cert minting,
#      auth gate, and reverse proxy wholesale rather than copying 330 lines of
#      TLS/WebSocket bridging. It is the first service-on-service dependency in
#      the tree, so it is declared explicitly in pyproject.toml and installed
#      here rather than left to chance.
#
#   2. The upstream binary — built by docker/build.sh, which owns the whole
#      story (why we compile rather than download, the pinned tag, the official
#      frontend assets). Skipped if already staged; pass --rebuild to force.
#
# Override the target env with AWM_ENV=<name>.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
WS="$(git -C "$HERE" rev-parse --show-toplevel)"
ENV="${AWM_ENV:-awm}"
VERSION="${CLAUDE_VIEW_VERSION:-0.45.0}"

REBUILD=0
for arg in "$@"; do
    case "$arg" in
        --rebuild) REBUILD=1 ;;
        *) echo "unknown flag: $arg" >&2; exit 2 ;;
    esac
done

run() { echo "+ pip install -e $*"; mamba run -n "$ENV" pip install -e "$@"; }

run "$WS/awm/service_components/config" --no-deps
run "$WS/awm/service_components/gatewayclient" --no-deps
run "$WS/awm/services/httpsfront" --no-deps
run "$WS/awm/services/claude-view"

# Bake the target env's absolute interpreter into a gitignored `.runtime-env`
# sidecar so the hub supervisor can respawn this service under systemd's
# minimal PATH (no `mamba`).
PYBIN="$(mamba run -n "$ENV" python -c 'import sys; print(sys.executable)')"
printf 'AWM_PYTHON=%s\nAWM_ENV_BIN=%s\n' "$PYBIN" "$(dirname "$PYBIN")" \
    > "$HERE/.runtime-env"

# -- the upstream server binary ---------------------------------------------
if [ "$REBUILD" = 1 ] || [ ! -x "$HERE/vendor/v$VERSION/claude-view" ]; then
    echo
    echo "Building the claude-view binary (docker; first run takes ~10 min)..."
    "$HERE/docker/build.sh"
else
    echo "claude-view v$VERSION already staged; pass --rebuild to force."
fi

echo "Installed awm-claude-view into env '$ENV'."
