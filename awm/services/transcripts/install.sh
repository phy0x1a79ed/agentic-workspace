#!/usr/bin/env bash
# Canonical install for the awm-transcripts service (editable, into the `awm` env).
#
# Installs the component libraries it imports and this service, then bakes the
# target env's absolute interpreter into a gitignored `.runtime-env` sidecar, so
# the supervisor can respawn under systemd's minimal PATH (no `mamba`).
#
# Override the target env with AWM_ENV=<name>.
set -euo pipefail
cd "$(dirname "$0")"
HERE="$(pwd)"
AWM="${AWM_ROOT:-$(git rev-parse --show-toplevel)/awm}"
ENV="${AWM_ENV:-awm}"
run() { echo "+ pip install -e $*"; mamba run -n "$ENV" pip install -e "$@"; }

run "$AWM/service_components/config" --no-deps
run "$AWM/service_components/persistence" --no-deps
run "$AWM/service_components/gatewayclient" --no-deps
run "$AWM/service_components/usage" --no-deps
run "$HERE"

PYBIN="$(mamba run -n "$ENV" python -c 'import sys; print(sys.executable)')"
printf 'AWM_PYTHON=%s\nAWM_ENV_BIN=%s\n' "$PYBIN" "$(dirname "$PYBIN")" > ./.runtime-env

echo "Installed awm-transcripts into env '$ENV'."
