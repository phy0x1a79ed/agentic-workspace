#!/usr/bin/env bash
# Build the wake plugin's own runtime: the conda env awm-wake, or AWM_ENV.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
AWM="${AWM_ROOT:-$(git -C "$HERE" rev-parse --show-toplevel)/awm}"
ENV="${AWM_ENV:-awm-wake}"

. "$AWM/plugin-env.sh"
run "$AWM/service_components/gatewayclient" --no-deps
run "$HERE"

bake_runtime_env

echo "Installed awm-wake into env '$ENV'."
