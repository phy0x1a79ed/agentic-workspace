#!/usr/bin/env bash
# Canonical install for the awm-mic service (editable, into the `awm` env).
#
# Installs the component libraries it imports (config, gatewayclient) first,
# then the service itself. mic is a thin pipe into `pacat` (no whisper/numpy),
# so the dependency surface is small. The one system dep, pulseaudio-utils for
# pacat, is NOT pip-installable — see INSTALL.md. Override the target env with
# AWM_ENV=<name>.
set -euo pipefail
WS="$(git -C "$(dirname "$0")" rev-parse --show-toplevel)"
ENV="${AWM_ENV:-awm}"
run() { echo "+ pip install -e $*"; mamba run -n "$ENV" pip install -e "$@"; }

run "$WS/awm/service_components/config" --no-deps
run "$WS/awm/service_components/gatewayclient" --no-deps
run "$WS/awm/services/mic"

# Bake the target env's absolute interpreter into a gitignored `.runtime-env`
# sidecar so the hub supervisor can respawn this service under systemd's
# minimal PATH (no `mamba`).
PYBIN="$(mamba run -n "$ENV" python -c 'import sys; print(sys.executable)')"
printf 'AWM_PYTHON=%s\nAWM_ENV_BIN=%s\n' "$PYBIN" "$(dirname "$PYBIN")" \
    > "$(dirname "$0")/.runtime-env"

echo "Installed awm-mic into env '$ENV'."
