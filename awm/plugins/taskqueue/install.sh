#!/usr/bin/env bash
# Install the task store into its own env (install.md R115 item 3). AWM_ENV overrides the env.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
WS="$(git -C "$HERE" rev-parse --show-toplevel)"
ENV="${AWM_ENV:-awm-queue}"
mamba env list | grep -q "^$ENV " || mamba create -y -n "$ENV" python=3.12 pip
mamba run -n "$ENV" pip install -e "$WS/awm/service_components/gatewayclient"
mamba run -n "$ENV" pip install -e "$HERE"
PYBIN="$(mamba run -n "$ENV" python -c 'import sys; print(sys.executable)')"
printf 'AWM_PYTHON=%s\nAWM_ENV_BIN=%s\n' "$PYBIN" "$(dirname "$PYBIN")" > "$HERE/.runtime-env"
