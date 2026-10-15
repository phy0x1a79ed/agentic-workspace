#!/usr/bin/env bash
# run.sh — self-contained entry point for the tether service.
#
# The gateway discovers this service by scanning awm/services/* for run.sh, and
# starts (and respawns) it by executing `bash run.sh`, injecting AWM_HUB_URL /
# AWM_SERVICE_NAME / AWM_SERVICE_ID into the environment.
#
# The adapter is the only Python here. Everything that touches a session is a
# tether binary fetched from the pinned release, parented to this process: on the
# operator's node the tether-operator daemon, on the public host the tether-relay.
# AWM_TETHER_ROLE selects which. Both die with this process, which is the point —
# a tether that survived its supervisor would be the persistence the scope forbids.
#
# fetch-release.sh pulls this host's role of the version RELEASE pins before the
# adapter registers. It runs before the control-WS lease, so a slow or failed
# fetch cannot trip the ready-ASAP reaper, and a failed fetch is non-fatal: the
# previous binaries keep running and `awm tether status` reports the version lag.
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
MODULE="awm.tether.hub_adapter"

# Fetch this host's pinned binaries before anything else. Non-fatal on failure.
bash ./fetch-release.sh || echo "tether: fetch-release.sh failed; see .awm/services/tether/fetch.log" >&2

if [ -n "${DEV_PYTHONPATH:-}" ]; then
    exec env PYTHONPATH="$DEV_PYTHONPATH" \
        mamba run -n "${AWM_ENV:-awm}" --no-capture-output \
        python -m "$MODULE"
fi

[ -f ./.runtime-env ] && . ./.runtime-env
[ -n "${AWM_ENV_BIN:-}" ] && export PATH="$AWM_ENV_BIN:$PATH"
exec "${AWM_PYTHON:-python}" -m "$MODULE"
