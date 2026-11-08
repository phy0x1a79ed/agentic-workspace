#!/usr/bin/env bash
# Canonical install for the awm-board service (editable, into the `awm` env).
# Override the target env with AWM_ENV=<name>.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
AWM="${AWM_ROOT:-$(git -C "$HERE" rev-parse --show-toplevel)/awm}"
ENV="${AWM_ENV:-awm}"
. "$AWM/plugin-env.sh"

run "$AWM/service_components/config" --no-deps
run "$AWM/service_components/persistence" --no-deps
run "$AWM/service_components/gatewayclient" --no-deps
run "$HERE"

bake_runtime_env

echo "Installed awm-board into env '$ENV'."
