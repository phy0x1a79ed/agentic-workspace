#!/usr/bin/env bash
# Entry point for the agents service. The hub spawns (and respawns) this
# script, injecting AWM_HUB_URL / AWM_SERVICE_NAME / AWM_SERVICE_ID (and
# optionally AWM_HUB_TOKEN) into the environment.
# `./.runtime-env` (written by install.sh, gitignored) bakes AWM_PYTHON =
# the target env's absolute interpreter, so the supervisor can respawn us
# under systemd's minimal PATH (no `mamba` on it). Falls back to bare
# `python` for interactive/dev use.
set -euo pipefail
cd "$(dirname "$0")"
[ -f ./.runtime-env ] && . ./.runtime-env
[ -n "${AWM_ENV_BIN:-}" ] && export PATH="$AWM_ENV_BIN:$PATH"
exec "${AWM_PYTHON:-python}" -m awm.agents.hub_adapter
