#!/usr/bin/env bash
# Build the 2fa plugin's own runtime: the conda env awm-2fa, or AWM_ENV.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
AWM="${AWM_ROOT:-$(git -C "$HERE" rev-parse --show-toplevel)/awm}"
ENV="${AWM_ENV:-awm-2fa}"

if ! command -v mamba >/dev/null; then
    conda_root="${AWM_CONDA_ROOT:-$HOME/miniforge3}"
    export PATH="$conda_root/condabin:$conda_root/bin:$PATH"
fi
mamba run -n "$ENV" true 2>/dev/null || mamba create -y -n "$ENV" -c conda-forge python=3.12 pip

run() { echo "+ pip install -e $*"; mamba run -n "$ENV" pip install -e "$@"; }
run "$AWM/service_components/config" --no-deps
run "$AWM/service_components/persistence" --no-deps
run "$AWM/service_components/gatewayclient" --no-deps
run "$HERE"

# The gateway respawns this service under systemd's minimal PATH, which has no mamba.
PYBIN="$(mamba run -n "$ENV" python -c 'import sys; print(sys.executable)')"
printf 'AWM_PYTHON=%s\nAWM_ENV_BIN=%s\n' "$PYBIN" "$(dirname "$PYBIN")" > "$HERE/.runtime-env"

echo "Installed awm-twofa into env '$ENV'."
