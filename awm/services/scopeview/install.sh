#!/usr/bin/env bash
# Install awm-scopeview editable into the awm env (AWM_ENV overrides).
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ENV="${AWM_ENV:-awm}"
REPO="$(git -C "$HERE" rev-parse --show-toplevel)"
AWM="${AWM_ROOT:-$REPO/awm}"

. "$AWM/plugin-env.sh"

run "$AWM/service_components/config" --no-deps
run "$AWM/service_components/gatewayclient" --no-deps
run "$HERE"

bake_runtime_env
echo "Installed awm-scopeview into env '$ENV'."
