#!/usr/bin/env bash
# Canonical install for the awm-rlm-factorio service (editable, into the `awm` env).
#
# Installs the component libraries it imports (config, persistence, gatewayclient, usage)
# first, then the service itself. Components have no install.sh of their own — they
# are pulled in here as plain editable dependencies. Override the target env with
# AWM_ENV=<name>.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
AWM="${AWM_ROOT:-$(git -C "$HERE" rev-parse --show-toplevel)/awm}"
ENV="${AWM_ENV:-awm}"
. "$AWM/plugin-env.sh"

run "$AWM/service_components/config" --no-deps
run "$AWM/service_components/persistence" --no-deps
run "$AWM/service_components/gatewayclient" --no-deps
run "$AWM/service_components/usage" --no-deps
run "$HERE"

bake_runtime_env

echo "Installed awm-rlm-factorio into env '$ENV'."
