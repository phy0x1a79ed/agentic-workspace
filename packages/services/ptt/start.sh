#!/usr/bin/env bash
# Entry point for the ptt service. Reads AWM_HUB_URL / AWM_HUB_TOKEN /
# AWM_SERVICE_NAME / AWM_SERVICE_ID from env (hub-injected) and runs the
# control-WS adapter.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec mamba run -n awm --no-capture-output \
  python -m backend.hub_adapter
