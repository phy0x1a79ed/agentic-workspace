#!/usr/bin/env bash
# run.sh — self-contained entry point for the transcripts service.
#
# The gateway discovers this service by scanning awm/services/* for run.sh, and
# starts (and respawns) it by executing `bash run.sh`, injecting AWM_HUB_URL /
# AWM_SERVICE_NAME / AWM_SERVICE_ID into the environment. No auth.
set -euo pipefail
cd "$(dirname "$0")"
MODULE="awm.transcripts.hub_adapter"

[ -f ./.runtime-env ] && . ./.runtime-env
[ -n "${AWM_ENV_BIN:-}" ] && export PATH="$AWM_ENV_BIN:$PATH"
exec "${AWM_PYTHON:-python}" -m "$MODULE"
