#!/usr/bin/env bash
# Canonical install for the awm-vpn service (editable, into the `awm` env).
#
# Installs the component libraries it imports (config, persistence, gatewayclient)
# first, then the service itself. Components have no install.sh of their own — they
# are pulled in here as plain editable dependencies. Override the target env with
# AWM_ENV=<name>.
#
# NOTE: this installs only the Python service. The per-profile VPN container
# images (containers/ubc, containers/proton) are a separate, operator-run
# `docker build` step — see INSTALL.md.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
AWM="${AWM_ROOT:-$(git -C "$HERE" rev-parse --show-toplevel)/awm}"
ENV="${AWM_ENV:-awm}"
run() { echo "+ pip install -e $*"; mamba run -n "$ENV" pip install -e "$@"; }

run "$AWM/service_components/config" --no-deps
run "$AWM/service_components/persistence" --no-deps
run "$AWM/service_components/gatewayclient" --no-deps
run "$HERE"

# Bake the target env's absolute interpreter into a gitignored `.runtime-env`
# sidecar so the hub supervisor can respawn this service under systemd's
# minimal PATH (no `mamba`).
PYBIN="$(mamba run -n "$ENV" python -c 'import sys; print(sys.executable)')"
printf 'AWM_PYTHON=%s\nAWM_ENV_BIN=%s\n' "$PYBIN" "$(dirname "$PYBIN")" \
    > "$HERE/.runtime-env"

echo "Installed awm-vpn into env '$ENV'."
