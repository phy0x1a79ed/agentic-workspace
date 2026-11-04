#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
MODULE="awm.rlm_browser.hub_adapter"

[ -f ./.runtime-env ] && . ./.runtime-env
[ -n "${AWM_ENV_BIN:-}" ] && export PATH="$AWM_ENV_BIN:$PATH"
exec "${AWM_PYTHON:-python}" -m "$MODULE"
