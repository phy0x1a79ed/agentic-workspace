#!/usr/bin/env bash
# Install the relay plugin into the target env (AWM_ENV, default awm), then
# fetch the tether relay binary and assets the tether component's RELEASE pins.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
AWM="${AWM_ROOT:-$(git -C "$HERE" rev-parse --show-toplevel)/awm}"
ENV="${AWM_ENV:-awm}"
run() { echo "+ pip install -e $*"; mamba run -n "$ENV" pip install -e "$@"; }

run "$AWM/service_components/gatewayclient" --no-deps
run "$AWM/service_components/tether" --no-deps
run "$HERE"

PYBIN="$(mamba run -n "$ENV" python -c 'import sys; print(sys.executable)')"
printf 'AWM_PYTHON=%s\nAWM_ENV_BIN=%s\n' "$PYBIN" "$(dirname "$PYBIN")" \
    > "$HERE/.runtime-env"

AWM_TETHER_ROLE=relay bash "$AWM/service_components/tether/fetch-release.sh" \
    || echo "fetch-release.sh failed; see services/tether/fetch.log under AWM_DIR" >&2

echo "Installed awm-relay into env '$ENV'."
