#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
[ -f ./.runtime-env ] && . ./.runtime-env
[ -n "${AWM_ENV_BIN:-}" ] && export PATH="$AWM_ENV_BIN:$PATH"
exec "${AWM_PYTHON:-python}" -m awm.browser_seats.hub_adapter
