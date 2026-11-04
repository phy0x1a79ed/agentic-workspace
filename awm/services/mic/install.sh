#!/usr/bin/env bash
# Canonical install for the awm-mic service (editable, into the `awm` env).
#
# Installs the component libraries it imports (config, gatewayclient, usage) first,
# then the service itself. mic is a thin pipe into `pacat` (no whisper/numpy),
# so the dependency surface is small. The one system dep, pulseaudio-utils for
# pacat, is NOT pip-installable — see INSTALL.md. Override the target env with
# AWM_ENV=<name>.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
AWM="${AWM_ROOT:-$(git -C "$HERE" rev-parse --show-toplevel)/awm}"
ENV="${AWM_ENV:-awm}"
run() { echo "+ pip install -e $*"; mamba run -n "$ENV" pip install -e "$@"; }

run "$AWM/service_components/config" --no-deps
run "$AWM/service_components/gatewayclient" --no-deps
run "$AWM/service_components/usage" --no-deps
run "$HERE"

# Bake the target env's absolute interpreter into a gitignored `.runtime-env`
# sidecar so the hub supervisor can respawn this service under systemd's
# minimal PATH (no `mamba`).
PYBIN="$(mamba run -n "$ENV" python -c 'import sys; print(sys.executable)')"
printf 'AWM_PYTHON=%s\nAWM_ENV_BIN=%s\n' "$PYBIN" "$(dirname "$PYBIN")" \
    > "$HERE/.runtime-env"

echo "Installed awm-mic into env '$ENV'."
