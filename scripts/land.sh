#!/usr/bin/env bash
# The land queue: submit, wait, batch, hold, bounce, status. See the skills ci/land
# and ci/merge. It runs the land module the awm env installs from this node's live
# checkout, so every branch lands by the same rules.
set -euo pipefail
conda=${AWM_CONDA_ROOT:-$HOME/miniforge3}
export PATH=$HOME/.local/bin:$conda/condabin:$conda/bin:$PATH
py=$(mamba run -n awm python -c 'import sys; print(sys.executable)' | grep -v '^[[:space:]]*$' | tail -1)
AWM_LAND_CWD=$PWD exec "$py" -u -m awm.routine.land "$@"
