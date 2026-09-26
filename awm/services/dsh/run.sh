#!/usr/bin/env bash
# run.sh — self-contained entry point for the dsh service.
#
# The gateway discovers this service by scanning awm/services/* for run.sh, and
# starts (and respawns) it by executing `bash run.sh`, injecting AWM_HUB_URL /
# AWM_SERVICE_NAME / AWM_SERVICE_ID into the environment.
#
# This service owns two things the gateway registration does not carry:
#   - the DeepSeek Harness web server, a node process parented to this one and
#     bound to loopback (see awm.dsh.harness),
#   - one HTTPS front on the mesh, reusing awm.httpsfront wholesale so the GUI
#     sits behind awm's edge session (see awm.dsh.front).
# Both die with this process, which is right: the harness persists its sessions
# to its own store, so a deploy costs a reconnect rather than a conversation.
#
# Two launch modes, branched on the dev signal DEV_PYTHONPATH:
#   - dev sandbox (DEV_PYTHONPATH set): run the uninstalled worktree code via
#     `mamba run`, resolving imports through the sandbox's PYTHONPATH dist-roots
#     — no install required.
#   - prod (installed): source ./.runtime-env (written by install.sh) for
#     AWM_PYTHON = the target env's absolute interpreter, so the supervisor can
#     respawn us under systemd's minimal PATH (no `mamba`).
set -euo pipefail
cd "$(dirname "$0")"
MODULE="awm.dsh.hub_adapter"

if [ -n "${DEV_PYTHONPATH:-}" ]; then
    exec env PYTHONPATH="$DEV_PYTHONPATH" \
        mamba run -n "${AWM_ENV:-awm}" --no-capture-output \
        python -m "$MODULE"
fi

[ -f ./.runtime-env ] && . ./.runtime-env
[ -n "${AWM_ENV_BIN:-}" ] && export PATH="$AWM_ENV_BIN:$PATH"
exec "${AWM_PYTHON:-python}" -m "$MODULE"
