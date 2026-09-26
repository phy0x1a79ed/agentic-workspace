#!/usr/bin/env bash
# Canonical install for the awm-penpot-view service (editable, into the `awm`
# env). Much shorter than drawio's install.sh: there is no web client to clone,
# patch, or build — this service is a Python process and nothing else, so the
# whole install is "editable-install the dists it needs" plus the .runtime-env
# sidecar every service writes.
#
# Override the target env with AWM_ENV=<name>.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
WS="$(git -C "$HERE" rev-parse --show-toplevel)"
ENV="${AWM_ENV:-awm}"

run() { echo "+ pip install -e $*"; mamba run -n "$ENV" pip install -e "$@"; }

run "$WS/awm/service_components/config" --no-deps
run "$WS/awm/service_components/gatewayclient" --no-deps
run "$WS/awm/services/penpot-view"

# Bake the target env's absolute interpreter into a gitignored `.runtime-env`
# sidecar so the hub supervisor can respawn this service under systemd's
# minimal PATH (no `mamba`).
PYBIN="$(mamba run -n "$ENV" python -c 'import sys; print(sys.executable)')"
printf 'AWM_PYTHON=%s\nAWM_ENV_BIN=%s\n' "$PYBIN" "$(dirname "$PYBIN")" \
    > "$HERE/.runtime-env"

echo "Installed awm-penpot-view into env '$ENV'."
echo "No static web-client tree and no per-user scaffolding — see INSTALL.md."
