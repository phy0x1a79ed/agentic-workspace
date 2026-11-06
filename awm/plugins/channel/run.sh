#!/usr/bin/env bash
# A member node runs the tether operator daemon, which the machine kind drives.
# Its pinned binary is fetched first, and a failed fetch is not fatal: the
# binary in place keeps running.
set -euo pipefail
cd "$(dirname "$0")"
MODULE="awm.channel.hub_adapter"
export AWM_TETHER_ROLE=operator
TETHER="$(cd ../../service_components/tether && pwd)"

bash "$TETHER/fetch-release.sh" || echo "channel: fetch-release.sh failed; see services/tether/fetch.log under AWM_DIR" >&2

[ -f ./.runtime-env ] && . ./.runtime-env
[ -n "${AWM_ENV_BIN:-}" ] && export PATH="$AWM_ENV_BIN:$PATH"
exec "${AWM_PYTHON:-python}" -m "$MODULE"
