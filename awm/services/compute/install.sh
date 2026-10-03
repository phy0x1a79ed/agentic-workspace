#!/usr/bin/env bash
# Canonical install for the awm-compute service (editable, into the `awm` env).
#
# Installs the component libraries it imports (config, gatewayclient,
# persistence) first, then the service itself. Everything the watchdog does is
# stdlib — /proc parsing, signals, sqlite — so there are no third-party deps: a
# service whose job is to not be the cause of an outage should not carry a
# dependency it can survive without. Override the target env with AWM_ENV=<name>.
#
# This does NOT install the Claude Code hooks; see INSTALL.md for that step,
# which edits a global settings file and is deliberately a separate decision.
set -euo pipefail
WS="$(git -C "$(dirname "$0")" rev-parse --show-toplevel)"
ENV="${AWM_ENV:-awm}"
run() { echo "+ pip install -e $*"; mamba run -n "$ENV" pip install -e "$@"; }

run "$WS/awm/service_components/config" --no-deps
run "$WS/awm/service_components/persistence" --no-deps
run "$WS/awm/service_components/gatewayclient" --no-deps
run "$WS/awm/services/compute"

# Bake the target env's absolute interpreter into a gitignored `.runtime-env`
# sidecar so the hub supervisor can respawn this service under systemd's
# minimal PATH (no `mamba`).
PYBIN="$(mamba run -n "$ENV" python -c 'import sys; print(sys.executable)')"
printf 'AWM_PYTHON=%s\nAWM_ENV_BIN=%s\n' "$PYBIN" "$(dirname "$PYBIN")" \
    > "$(dirname "$0")/.runtime-env"

echo "Installed awm-compute into env '$ENV'."
