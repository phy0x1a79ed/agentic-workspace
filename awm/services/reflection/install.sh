#!/usr/bin/env bash
# Canonical install for the awm-reflection service (editable, into the `awm` env).
#
# Installs the component libraries it imports (config, gatewayclient) first, then
# the service itself. This service owns no database, so it does NOT depend on
# awm-persistence. Components have no install.sh of their own — they are pulled in
# here as plain editable dependencies. Override the target env with AWM_ENV=<name>.
set -euo pipefail
WS="$(git -C "$(dirname "$0")" rev-parse --show-toplevel)"
ENV="${AWM_ENV:-awm}"
run() { echo "+ pip install -e $*"; mamba run -n "$ENV" pip install -e "$@"; }

run "$WS/awm/service_components/config" --no-deps
run "$WS/awm/service_components/gatewayclient" --no-deps
run "$WS/awm/services/reflection"

# Bake the target env's absolute interpreter into a gitignored `.runtime-env`
# sidecar so the hub supervisor can respawn this service under systemd's
# minimal PATH (no `mamba`).
PYBIN="$(mamba run -n "$ENV" python -c 'import sys; print(sys.executable)')"
printf 'AWM_PYTHON=%s\nAWM_ENV_BIN=%s\n' "$PYBIN" "$(dirname "$PYBIN")" \
    > "$(dirname "$0")/.runtime-env"

echo "Installed awm-reflection into env '$ENV'."
