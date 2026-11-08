#!/usr/bin/env bash
# Build the ssh plugin's own runtime: the conda env awm-ssh, or AWM_ENV.
# All SSH operations use the system ssh binary, so there are no third-party deps.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
AWM="${AWM_ROOT:-$(git -C "$HERE" rev-parse --show-toplevel)/awm}"
ENV="${AWM_ENV:-awm-ssh}"

. "$AWM/plugin-env.sh"
run "$AWM/service_components/config" --no-deps
run "$AWM/service_components/persistence" --no-deps
run "$AWM/service_components/gatewayclient" --no-deps
run "$HERE"

bake_runtime_env

echo "Installed awm-ssh into env '$ENV'."
