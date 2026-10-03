#!/usr/bin/env bash
# run.sh — self-contained entry point for the dvc service.
#
# The gateway discovers this service by scanning awm/services/* for run.sh, and
# starts (and respawns) it by executing `bash run.sh`, injecting AWM_HUB_URL /
# AWM_SERVICE_NAME / AWM_SERVICE_ID into the environment. No auth.
#
# Two launch modes, branched on the dev signal DEV_PYTHONPATH:
#   - dev sandbox: run the uninstalled worktree code via `mamba run`, resolving
#     imports through the sandbox's PYTHONPATH dist-roots — no install required.
#   - prod (installed): source ./.runtime-env (written by install.sh) for
#     AWM_PYTHON = the target env's absolute interpreter, so the supervisor can
#     respawn us under systemd's minimal PATH (no `mamba`).
#
# The `globus` CLI lives in its OWN env and is invoked as a subprocess, so its
# absolute path is baked into .runtime-env as AWM_DVC_GLOBUS_BIN for the same
# reason — a bare `globus` resolves interactively and vanishes on respawn.
set -euo pipefail
cd "$(dirname "$0")"
MODULE="awm.dvc.hub_adapter"

if [ -n "${DEV_PYTHONPATH:-}" ]; then
    [ -z "${AWM_DVC_GLOBUS_BIN:-}" ] && [ -f ./.runtime-env ] && . ./.runtime-env
    if [ -z "${AWM_DVC_GLOBUS_BIN:-}" ]; then
        AWM_DVC_GLOBUS_BIN="$(mamba run -n "${GLOBUS_ENV:-globus}" which globus 2>/dev/null || true)"
    fi
    [ -n "${AWM_DVC_GLOBUS_BIN:-}" ] && export AWM_DVC_GLOBUS_BIN
    exec env PYTHONPATH="$DEV_PYTHONPATH" \
        mamba run -n "${AWM_ENV:-awm}" --no-capture-output \
        python -m "$MODULE"
fi

[ -f ./.runtime-env ] && . ./.runtime-env
[ -n "${AWM_ENV_BIN:-}" ] && export PATH="$AWM_ENV_BIN:$PATH"
[ -n "${AWM_DVC_GLOBUS_BIN:-}" ] && export AWM_DVC_GLOBUS_BIN
exec "${AWM_PYTHON:-python}" -m "$MODULE"
