#!/usr/bin/env bash
# Entry point for the discord service. The hub spawns (and respawns) this
# script, injecting AWM_HUB_URL / AWM_SERVICE_NAME / AWM_SERVICE_ID (and
# optionally AWM_HUB_TOKEN) into the environment. The adapter uses them to
# POST /hub/service/register and then hold the control WS open.
#
# Relies on `awm-discord` (and its awm-* component deps) being installed into
# the target env via ./install.sh — `python -m awm.discord.hub_adapter` then
# resolves through the installed namespace packages.
set -euo pipefail
exec mamba run -n "${AWM_ENV:-awm}" --no-capture-output \
  python -m awm.discord.hub_adapter
