#!/usr/bin/env bash
# Canonical install for the awm-dsh service (editable, into the `awm` env), plus
# the node runtime the DeepSeek Harness itself needs.
#
# Three halves, because this service has three kinds of dependency:
#
#   1. Python — the adapter, the supervisor and the mesh front. Installs the
#      component libraries it imports first, then httpsfront (the front is a
#      *configuration* of that component, not a copy of it), then the service.
#
#   2. A node toolchain — its own mamba env pinned to node 24. The harness needs
#      >= 22.19 and the node on the host PATH is older and carries no npm, so
#      borrowing it is not an option.
#
#   3. The harness — installed from npm rather than built from source: the
#      published frontend package ships its built `dist/`, so the upstream
#      pnpm build apparatus buys nothing here. It lands in the service's *state*
#      directory, not the git tree, because a 277 MB node_modules is state.
#
# Every step is idempotent and skips itself when already satisfied, because this
# script runs on every deploy (awm/gateway/install.sh invokes each service's).
#
# Override the target env with AWM_ENV=<name>.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
WS="$(git -C "$HERE" rev-parse --show-toplevel)"
ENV="${AWM_ENV:-awm}"

# dsh is an explicit developer preview that promises compatibility-breaking
# changes, so the version is pinned and bumping it is a deliberate step.
DSH_VERSION="${DSH_VERSION:-0.1.0-rc.7}"
NODE_ENV_NAME="${DSH_NODE_ENV:-dsh}"
STATE_DIR="${DSH_STATE_DIR:-$WS/.awm/services/dsh}"
RUNTIME_DIR="$STATE_DIR/runtime"

run() { echo "+ pip install -e $*"; mamba run -n "$ENV" pip install -e "$@"; }

run "$WS/awm/service_components/config" --no-deps
run "$WS/awm/service_components/gatewayclient" --no-deps
run "$WS/awm/services/httpsfront" --no-deps
run "$WS/awm/services/dsh"

# Bake the target env's absolute interpreter into a gitignored `.runtime-env`
# sidecar so the hub supervisor can respawn this service under systemd's
# minimal PATH (no `mamba`).
PYBIN="$(mamba run -n "$ENV" python -c 'import sys; print(sys.executable)')"
printf 'AWM_PYTHON=%s\nAWM_ENV_BIN=%s\n' "$PYBIN" "$(dirname "$PYBIN")" \
    > "$HERE/.runtime-env"

# -- the node toolchain -----------------------------------------------------
if mamba run -n "$NODE_ENV_NAME" node --version >/dev/null 2>&1; then
    echo "node env '$NODE_ENV_NAME' present: $(mamba run -n "$NODE_ENV_NAME" node --version)."
else
    echo "Creating node env '$NODE_ENV_NAME' (nodejs 24) …"
    mamba create -y -n "$NODE_ENV_NAME" -c conda-forge 'nodejs>=24,<25'
fi

NODE_BIN="$(dirname "$(mamba run -n "$NODE_ENV_NAME" node -p 'process.execPath')")"
mkdir -p "$STATE_DIR"
# The supervisor respawns under systemd's minimal PATH, where neither `node` nor
# the `mamba` that could find it exists. Recording the absolute directory here
# is what lets it build a working PATH for the child.
printf '%s\n' "$NODE_BIN" > "$STATE_DIR/node-bin"

# -- the harness ------------------------------------------------------------
HAVE=""
if [ -f "$RUNTIME_DIR/node_modules/@deepseek-ai/dsh/package.json" ]; then
    HAVE="$("$NODE_BIN/node" -p \
        "require('$RUNTIME_DIR/node_modules/@deepseek-ai/dsh/package.json').version" \
        2>/dev/null || true)"
fi

if [ "$HAVE" = "$DSH_VERSION" ]; then
    echo "Harness present at $DSH_VERSION."
else
    echo "Installing @deepseek-ai/dsh@$DSH_VERSION into $RUNTIME_DIR …"
    mkdir -p "$RUNTIME_DIR"
    [ -f "$RUNTIME_DIR/package.json" ] || printf '%s\n' \
        '{"name":"awm-dsh-runtime","private":true,"description":"npm install target for the harness the awm dsh service supervises."}' \
        > "$RUNTIME_DIR/package.json"
    # npm resolves peers itself: --legacy-peer-deps installs a tree that is
    # missing @deepseek-ai/cordis-plugin-group and fails at first launch with
    # ERR_MODULE_NOT_FOUND. The full resolution is slow; it is also correct.
    ( cd "$RUNTIME_DIR" && PATH="$NODE_BIN:$PATH" \
        NODE_OPTIONS="${NODE_OPTIONS:---max-old-space-size=8192}" \
        "$NODE_BIN/npm" install --no-fund --no-audit "@deepseek-ai/dsh@$DSH_VERSION" )
fi

echo "Installed awm-dsh into env '$ENV'."
echo "Runtime:  $RUNTIME_DIR"
echo "DSH_HOME: $STATE_DIR/home"
