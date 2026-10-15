#!/usr/bin/env bash
# run.sh — self-contained entry point for the cx service.
#
# The gateway discovers this folder by scanning awm/services/* for run.sh and
# starts (and respawns) it with `bash run.sh`, injecting AWM_HUB_URL /
# AWM_SERVICE_NAME / AWM_SERVICE_ID. The adapter reads those, registers, and
# holds the control WS open. No auth.
#
# Two launch modes, branched on the dev signal DEV_PYTHONPATH:
#   - dev sandbox (DEV_PYTHONPATH set): run the uninstalled worktree code via
#     `mamba run`, resolving imports through the sandbox's PYTHONPATH dist-roots.
#   - prod (installed): source ./.runtime-env for AWM_PYTHON = the env's absolute
#     interpreter so the supervisor can respawn us under systemd's minimal PATH.
set -euo pipefail
cd "$(dirname "$0")"
MODULE="awm.cx.hub_adapter"

if [ -n "${DEV_PYTHONPATH:-}" ]; then
    exec env PYTHONPATH="$DEV_PYTHONPATH" \
        mamba run -n "${AWM_ENV:-awm}" --no-capture-output \
        python -m "$MODULE"
fi

[ -f ./.runtime-env ] && . ./.runtime-env
[ -n "${AWM_ENV_BIN:-}" ] && export PATH="$AWM_ENV_BIN:$PATH"
exec "${AWM_PYTHON:-python}" -m "$MODULE"
