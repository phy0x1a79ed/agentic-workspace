#!/usr/bin/env bash
# Build the zotero plugin's own runtime: the conda env awm-zotero, or AWM_ENV.
#
# This service owns no database: its state is the bundle in the vault scope, so
# it does not depend on awm-persistence. The library is read with `curl` over
# `ssh` on the node the Zotero desktop is on, and the vault is reached over the
# trilium service's verbs: no client library, no credential, no daemon.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
AWM="${AWM_ROOT:-$(git -C "$HERE" rev-parse --show-toplevel)/awm}"
ENV="${AWM_ENV:-awm-zotero}"

if ! command -v mamba >/dev/null; then
    conda_root="${AWM_CONDA_ROOT:-$HOME/miniforge3}"
    export PATH="$conda_root/condabin:$conda_root/bin:$PATH"
fi
mamba run -n "$ENV" true 2>/dev/null || mamba create -y -n "$ENV" -c conda-forge python=3.12 pip

run() { echo "+ pip install -e $*"; mamba run -n "$ENV" pip install -e "$@"; }
run "$AWM/service_components/config" --no-deps
run "$AWM/service_components/gatewayclient" --no-deps
run "$AWM/service_components/grantlib"
run "$HERE"

# The gateway respawns this service under systemd's minimal PATH, which has no mamba.
PYBIN="$(mamba run -n "$ENV" python -c 'import sys; print(sys.executable)')"
printf 'AWM_PYTHON=%s\nAWM_ENV_BIN=%s\n' "$PYBIN" "$(dirname "$PYBIN")" > "$HERE/.runtime-env"

echo "Installed awm-zotero into env '$ENV'."
