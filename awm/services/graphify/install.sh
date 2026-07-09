#!/usr/bin/env bash
# Canonical install for the awm-graphify service (editable, into the `awm` env).
#
# Installs the component libraries it imports (config, gatewayclient) and this
# service into the `awm` env, then provisions the `graphify` CLI into its OWN
# isolated env (its 30+ tree-sitter grammars + numpy must NOT pollute `awm`) and
# bakes the resulting interpreter + graphify binary paths into a gitignored
# `.runtime-env` sidecar so the supervisor can respawn under systemd's minimal
# PATH (no `mamba`). Override the target env with AWM_ENV=<name>, the graphify
# env with GRAPHIFY_ENV=<name>.
set -euo pipefail
cd "$(dirname "$0")"
WS="$(git rev-parse --show-toplevel)"
ENV="${AWM_ENV:-awm}"
GENV="${GRAPHIFY_ENV:-graphify}"
run() { echo "+ pip install -e $*"; mamba run -n "$ENV" pip install -e "$@"; }

run "$WS/awm/service_components/config" --no-deps
run "$WS/awm/service_components/gatewayclient" --no-deps
run "$WS/awm/services/graphify"

# Provision the graphify CLI into its own env (idempotent — skip if already
# present). Kept out of `awm` because graphifyy pulls 30+ tree-sitter wheels.
if ! mamba run -n "$GENV" graphify --version >/dev/null 2>&1; then
    echo "+ provisioning graphify CLI into env '$GENV'"
    mamba create -y -n "$GENV" python=3.11 >/dev/null
    # Pinned: the runner's argv + graph.json contract was reverse-engineered
    # against graphify 0.9.1. An unpinned bump could silently change the CLI
    # surface or output shape; revisit the runner before moving this pin.
    mamba run -n "$GENV" pip install 'graphifyy==0.9.1'
fi
GBIN="$(mamba run -n "$GENV" python -c 'import shutil; print(shutil.which("graphify"))')"

# Bake the absolute interpreter + graphify binary into the gitignored sidecar.
PYBIN="$(mamba run -n "$ENV" python -c 'import sys; print(sys.executable)')"
printf 'AWM_PYTHON=%s\nAWM_ENV_BIN=%s\nGRAPHIFY_BIN=%s\n' \
    "$PYBIN" "$(dirname "$PYBIN")" "$GBIN" > ./.runtime-env

echo "Installed awm-graphify into env '$ENV' (graphify CLI: $GBIN)."
