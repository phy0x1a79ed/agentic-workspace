#!/usr/bin/env bash
# run.sh — self-contained entry point for the claude-science service.
#
# The gateway discovers this service by scanning awm/services/* for run.sh, and
# starts (and respawns) it by executing `bash run.sh`, injecting AWM_HUB_URL /
# AWM_SERVICE_NAME / AWM_SERVICE_ID into the environment. The adapter reads
# those, registers, and holds the control WS open. No auth.
#
# This service owns three things the gateway registration does not carry:
#   - the Claude Science workbench daemon, a foreign binary that is *adopted*
#     rather than parented (see awm.claude_science.daemon for why),
#   - two HTTPS fronts on the mesh, one per workbench origin, reusing
#     awm.httpsfront wholesale so both sit behind awm's edge session, and
#   - an MCP bridge that hands the workbench a curated slice of awm's verbs.
# The fronts and the bridge die with this process; the daemon deliberately does
# not, so an awm deploy never interrupts a conversation.
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
MODULE="awm.claude_science.hub_adapter"

if [ -n "${DEV_PYTHONPATH:-}" ]; then
    exec env PYTHONPATH="$DEV_PYTHONPATH" \
        mamba run -n "${AWM_ENV:-awm}" --no-capture-output \
        python -m "$MODULE"
fi

[ -f ./.runtime-env ] && . ./.runtime-env
[ -n "${AWM_ENV_BIN:-}" ] && export PATH="$AWM_ENV_BIN:$PATH"
exec "${AWM_PYTHON:-python}" -m "$MODULE"
