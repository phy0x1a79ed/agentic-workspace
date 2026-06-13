#!/usr/bin/env bash
# Entry point for the artifacts service. The hub spawns (and respawns) this
# script, injecting AWM_HUB_URL / AWM_SERVICE_NAME / AWM_SERVICE_ID (and
# optionally AWM_HUB_TOKEN) into the environment. The adapter uses them to
# POST /hub/service/register and then hold the control WS open.
#
# Relies on `awm-artifacts` (and its awm-* component deps) being installed into
# the target env via ./install.sh — `python -m awm.artifacts.hub_adapter` then
# resolves through the installed namespace packages.
# `./.runtime-env` (written by install.sh, gitignored) bakes AWM_PYTHON =
# the target env's absolute interpreter, so the supervisor can respawn us
# under systemd's minimal PATH (no `mamba` on it). Falls back to bare
# `python` for interactive/dev use.
set -euo pipefail
cd "$(dirname "$0")"
[ -f ./.runtime-env ] && . ./.runtime-env
[ -n "${AWM_ENV_BIN:-}" ] && export PATH="$AWM_ENV_BIN:$PATH"
exec "${AWM_PYTHON:-python}" -m awm.artifacts.hub_adapter
