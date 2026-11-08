#!/usr/bin/env bash
# Build the vpn plugin's own runtime: the conda env awm-vpn, or AWM_ENV.
# This installs only the Python service. The per-profile VPN container images
# (containers/ubc, containers/proton) are an operator-run `docker build`, see INSTALL.md.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
AWM="${AWM_ROOT:-$(git -C "$HERE" rev-parse --show-toplevel)/awm}"
ENV="${AWM_ENV:-awm-vpn}"

. "$AWM/plugin-env.sh"

run "$AWM/service_components/config" --no-deps
run "$AWM/service_components/persistence" --no-deps
run "$AWM/service_components/gatewayclient" --no-deps
run "$HERE"

bake_runtime_env

echo "Installed awm-vpn into env '$ENV'."
