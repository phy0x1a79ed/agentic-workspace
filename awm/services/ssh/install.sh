#!/usr/bin/env bash
# Build the ssh plugin's own runtime: the conda env awm-ssh, or AWM_ENV.
# All SSH operations use the system ssh binary, so there are no third-party deps.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
AWM="${AWM_ROOT:-$(git -C "$HERE" rev-parse --show-toplevel)/awm}"
ENV="${AWM_ENV:-awm-ssh}"

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

echo "Installed awm-ssh into env '$ENV'."
