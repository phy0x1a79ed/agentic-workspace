#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
AWM="${AWM_ROOT:-$(git -C "$HERE" rev-parse --show-toplevel)/awm}"
ENV="${AWM_ENV:-awm-ssh-slot}"

. "$AWM/plugin-env.sh"
run "$AWM/service_components/config" --no-deps
run "$AWM/service_components/persistence" --no-deps
run "$AWM/service_components/gatewayclient" --no-deps
run "$AWM/services/ssh" --no-deps
run "$HERE"

bake_runtime_env

echo "Installed awm-ssh-slot into env '$ENV'."
