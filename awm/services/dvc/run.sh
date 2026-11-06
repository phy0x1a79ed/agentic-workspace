#!/usr/bin/env bash
# run.sh — self-contained entry point for the dvc service.
#
# The gateway discovers this service by scanning awm/services/* for run.sh, and
# starts (and respawns) it by executing `bash run.sh`, injecting AWM_HUB_URL /
# AWM_SERVICE_NAME / AWM_SERVICE_ID into the environment. No auth.
#
# The `globus` CLI lives in its own env and runs as a subprocess, so install.sh
# writes its absolute path into .runtime-env as AWM_DVC_GLOBUS_BIN: a bare
# `globus` resolves interactively and vanishes under systemd's minimal PATH.
set -euo pipefail
cd "$(dirname "$0")"
MODULE="awm.dvc.hub_adapter"

[ -f ./.runtime-env ] && . ./.runtime-env
[ -n "${AWM_ENV_BIN:-}" ] && export PATH="$AWM_ENV_BIN:$PATH"
[ -n "${AWM_DVC_GLOBUS_BIN:-}" ] && export AWM_DVC_GLOBUS_BIN
exec "${AWM_PYTHON:-python}" -m "$MODULE"
