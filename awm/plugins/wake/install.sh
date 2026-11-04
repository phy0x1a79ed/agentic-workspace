#!/usr/bin/env bash
# Install the wake plugin into the awm env (override with AWM_ENV=<name>).
set -euo pipefail
WS="$(git -C "$(dirname "$0")" rev-parse --show-toplevel)"
ENV="${AWM_ENV:-awm}"
mamba run -n "$ENV" pip install -e "$WS/awm/service_components/gatewayclient" --no-deps
mamba run -n "$ENV" pip install -e "$(dirname "$0")"
PYBIN="$(mamba run -n "$ENV" python -c 'import sys; print(sys.executable)')"
printf 'AWM_PYTHON=%s\nAWM_ENV_BIN=%s\n' "$PYBIN" "$(dirname "$PYBIN")" > "$(dirname "$0")/.runtime-env"
