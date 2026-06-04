#!/usr/bin/env bash
# Entry point for the tts service. The hub injects AWM_HUB_URL,
# AWM_HUB_TOKEN, AWM_SERVICE_NAME, AWM_SERVICE_ID in env; the adapter
# uses them to POST /hub/service/register and then hold a control WS.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec mamba run -n awm --no-capture-output \
  python -m tts.backend.hub_adapter
