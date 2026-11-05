#!/usr/bin/env bash
# Install the channel plugin into the target env (AWM_ENV, default awm), with
# its dependencies (spake2 for room encryption among them), then fetch the
# tether binaries RELEASE pins.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
AWM="${AWM_ROOT:-$(git -C "$HERE" rev-parse --show-toplevel)/awm}"
ENV="${AWM_ENV:-awm}"
run() { echo "+ pip install -e $*"; mamba run -n "$ENV" pip install -e "$@"; }

run "$AWM/service_components/gatewayclient" --no-deps
run "$HERE"
mamba run -n "$ENV" python -c 'import spake2' \
    || { echo "spake2 did not install into env '$ENV'" >&2; exit 1; }

PYBIN="$(mamba run -n "$ENV" python -c 'import sys; print(sys.executable)')"
printf 'AWM_PYTHON=%s\nAWM_ENV_BIN=%s\n' "$PYBIN" "$(dirname "$PYBIN")" \
    > "$HERE/.runtime-env"

bash "$HERE/fetch-release.sh" || echo "fetch-release.sh failed; see .awm/services/channel/tether/fetch.log" >&2

echo "Installed awm-channel into env '$ENV'."
