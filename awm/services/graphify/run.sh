#!/usr/bin/env bash
# run.sh — self-contained entry point for the graphify service.
#
# The gateway discovers this service by scanning awm/services/* for run.sh, and
# starts (and respawns) it by executing `bash run.sh`, injecting AWM_HUB_URL /
# AWM_SERVICE_NAME / AWM_SERVICE_ID into the environment. The adapter reads
# those, POSTs /hub/service/register, and holds the control WS open. No auth.
#
# Two launch modes, branched on the dev signal DEV_PYTHONPATH:
#   - dev sandbox (DEV_PYTHONPATH set): run the uninstalled worktree code via
#     `mamba run`, resolving imports through the sandbox's PYTHONPATH dist-roots
#     — no install required. The graphify binary lives in its own env (not the
#     awm env this runs under), so we resolve GRAPHIFY_BIN here: an explicit
#     GRAPHIFY_BIN wins, else a baked ./.runtime-env, else the isolated
#     ${GRAPHIFY_ENV:-graphify} env. If none resolves we still launch — the
#     service raises a clear error at first build, not at boot.
#   - prod (installed): source ./.runtime-env (written by install.sh) for
#     AWM_PYTHON = the target env's absolute interpreter and GRAPHIFY_BIN = the
#     graphify executable in its own isolated env, so the supervisor can respawn
#     us under systemd's minimal PATH (no `mamba`).
set -euo pipefail
cd "$(dirname "$0")"
MODULE="awm.graphify.hub_adapter"

if [ -n "${DEV_PYTHONPATH:-}" ]; then
    # The graphify CLI is not in the awm env we run under; resolve it.
    [ -z "${GRAPHIFY_BIN:-}" ] && [ -f ./.runtime-env ] && . ./.runtime-env
    if [ -z "${GRAPHIFY_BIN:-}" ]; then
        GRAPHIFY_BIN="$(mamba run -n "${GRAPHIFY_ENV:-graphify}" which graphify 2>/dev/null || true)"
    fi
    [ -n "${GRAPHIFY_BIN:-}" ] && export GRAPHIFY_BIN
    exec env PYTHONPATH="$DEV_PYTHONPATH" \
        mamba run -n "${AWM_ENV:-awm}" --no-capture-output \
        python -m "$MODULE"
fi

[ -f ./.runtime-env ] && . ./.runtime-env
[ -n "${AWM_ENV_BIN:-}" ] && export PATH="$AWM_ENV_BIN:$PATH"
[ -n "${GRAPHIFY_BIN:-}" ] && export GRAPHIFY_BIN
exec "${AWM_PYTHON:-python}" -m "$MODULE"
