#!/usr/bin/env bash
# Build the 2fa plugin's own runtime: the conda env awm-2fa, or AWM_ENV.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
AWM="${AWM_ROOT:-$(git -C "$HERE" rev-parse --show-toplevel)/awm}"
ENV="${AWM_ENV:-awm-2fa}"

. "$AWM/plugin-env.sh"
run "$AWM/service_components/config" --no-deps
run "$AWM/service_components/persistence" --no-deps
run "$AWM/service_components/gatewayclient" --no-deps
run "$HERE"

bake_runtime_env

echo "Installed awm-twofa into env '$ENV'."
