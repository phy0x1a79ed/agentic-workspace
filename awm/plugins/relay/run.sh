#!/usr/bin/env bash
# The public host supervises the tether relay. Its pinned binaries are fetched
# first, and a failed fetch is not fatal: the binaries in place keep running.
set -euo pipefail
cd "$(dirname "$0")"
export AWM_TETHER_ROLE=relay
TETHER="$(cd ../../service_components/tether && pwd)"

bash "$TETHER/fetch-release.sh" || echo "relay: fetch-release.sh failed; see services/tether/fetch.log under AWM_DIR" >&2

[ -f ./.runtime-env ] && . ./.runtime-env
[ -n "${AWM_ENV_BIN:-}" ] && export PATH="$AWM_ENV_BIN:$PATH"
exec "${AWM_PYTHON:-python}" -m awm.relay.hub_adapter
