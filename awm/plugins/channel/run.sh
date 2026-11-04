#!/usr/bin/env bash
# The tether binaries the machine kind supervises are fetched first, and a
# failed fetch is not fatal: the binaries in place keep running.
set -euo pipefail
cd "$(dirname "$0")"
MODULE="awm.channel.hub_adapter"

bash ./fetch-release.sh || echo "channel: fetch-release.sh failed; see .awm/services/tether/fetch.log" >&2

[ -f ./.runtime-env ] && . ./.runtime-env
[ -n "${AWM_ENV_BIN:-}" ] && export PATH="$AWM_ENV_BIN:$PATH"
exec "${AWM_PYTHON:-python}" -m "$MODULE"
