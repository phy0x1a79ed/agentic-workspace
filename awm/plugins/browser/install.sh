#!/usr/bin/env bash
# Install the browser singleton into the awm env. AWM_ENV overrides the env.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
WS="$(git -C "$HERE" rev-parse --show-toplevel)"
ENV="${AWM_ENV:-awm}"
for pkg in awm/service_components/config awm/service_components/gatewayclient awm/service_components/trustpolicy; do
    mamba run -n "$ENV" pip install -e "$WS/$pkg"
done
mamba run -n "$ENV" pip install -e "$HERE"
PYBIN="$(mamba run -n "$ENV" python -c 'import sys; print(sys.executable)')"
printf 'AWM_PYTHON=%s\nAWM_ENV_BIN=%s\n' "$PYBIN" "$(dirname "$PYBIN")" > "$HERE/.runtime-env"
