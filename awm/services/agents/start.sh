#!/usr/bin/env bash
# Entry point for the agents service. The hub spawns (and respawns) this
# script, injecting AWM_HUB_URL / AWM_SERVICE_NAME / AWM_SERVICE_ID (and
# optionally AWM_HUB_TOKEN) into the environment.
set -euo pipefail
exec mamba run -n "${AWM_ENV:-awm}" --no-capture-output \
  python -m awm.agents.hub_adapter
