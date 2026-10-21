#!/usr/bin/env bash
# Install the keep service (editable, into the `awm` env) and the two component
# libraries it imports. Override the target env with AWM_ENV=<name>.
set -euo pipefail
WS="$(git -C "$(dirname "$0")" rev-parse --show-toplevel)"
ENV="${AWM_ENV:-awm}"
run() { echo "+ pip install -e $*"; mamba run -n "$ENV" pip install -e "$@"; }

run "$WS/awm/service_components/config" --no-deps
run "$WS/awm/service_components/gatewayclient" --no-deps
run "$WS/awm/service_components/claudedaemon" --no-deps
run "$WS/awm/services/keep"

PYBIN="$(mamba run -n "$ENV" python -c 'import sys; print(sys.executable)')"
printf 'AWM_PYTHON=%s\nAWM_ENV_BIN=%s\n' "$PYBIN" "$(dirname "$PYBIN")" \
    > "$(dirname "$0")/.runtime-env"

echo "Installed awm-keep into env '$ENV'."
