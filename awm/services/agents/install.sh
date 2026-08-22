#!/usr/bin/env bash
# Canonical install for the awm-agents service (editable, into the `awm` env).
set -euo pipefail
WS="$(git -C "$(dirname "$0")" rev-parse --show-toplevel)"
ENV="${AWM_ENV:-awm}"
run() { echo "+ pip install -e $*"; mamba run -n "$ENV" pip install -e "$@"; }

run "$WS/awm/service_components/config" --no-deps
run "$WS/awm/service_components/persistence" --no-deps
run "$WS/awm/service_components/gatewayclient" --no-deps
# agentcore is a leaf component (no install.sh) the agents service drives for
# the harness subprocess; editable-install it before agents so the dep resolves.
run "$WS/awm/service_components/agentcore" --no-deps
run "$WS/awm/services/agents"

# Bake the target env's absolute interpreter into a gitignored `.runtime-env`
# sidecar so the hub supervisor can respawn this service under systemd's
# minimal PATH (no `mamba`).
PYBIN="$(mamba run -n "$ENV" python -c 'import sys; print(sys.executable)')"
printf 'AWM_PYTHON=%s\nAWM_ENV_BIN=%s\n' "$PYBIN" "$(dirname "$PYBIN")" \
    > "$(dirname "$0")/.runtime-env"

echo "Installed awm-agents into env '$ENV'."
