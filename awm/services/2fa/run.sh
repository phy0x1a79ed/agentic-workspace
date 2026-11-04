#!/usr/bin/env bash
# run.sh — self-contained entry point for the 2fa service.
#
# The gateway discovers this service by scanning awm/services/* for run.sh, and
# starts (and respawns) it by executing `bash run.sh`, injecting AWM_HUB_URL /
# AWM_SERVICE_NAME / AWM_SERVICE_ID into the environment. The adapter reads
# those, POSTs /hub/service/register, and holds the control WS open. No auth.
#
# The service folder is `2fa` (the gateway service name + the `2fa_*` verb
# domain), but the Python package is `awm.twofa` — `awm.2fa` is not a valid
# module identifier (leading digit). Only the import name differs.
set -euo pipefail
cd "$(dirname "$0")"
MODULE="awm.twofa.hub_adapter"

[ -f ./.runtime-env ] && . ./.runtime-env
[ -n "${AWM_ENV_BIN:-}" ] && export PATH="$AWM_ENV_BIN:$PATH"
exec "${AWM_PYTHON:-python}" -m "$MODULE"
