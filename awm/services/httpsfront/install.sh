#!/usr/bin/env bash
# Canonical install for the awm-httpsfront service (editable, into the `awm` env).
#
# Installs the component libraries it imports (config, gatewayclient, grantlib,
# tlsfront) first, then the service itself. The proxy is built on
# starlette/uvicorn/httpx/websockets,
# which the gateway already depends on — so in the `awm` env they resolve as
# already-satisfied. System dep: openssl (mint the CA + leaf) — see INSTALL.md.
# Override the target env with AWM_ENV=<name>.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
AWM="${AWM_ROOT:-$(git -C "$HERE" rev-parse --show-toplevel)/awm}"
ENV="${AWM_ENV:-awm}"
run() { echo "+ pip install -e $*"; mamba run -n "$ENV" pip install -e "$@"; }

run "$AWM/service_components/config" --no-deps
run "$AWM/service_components/gatewayclient" --no-deps
run "$AWM/service_components/grantlib" --no-deps
run "$AWM/service_components/tlsfront" --no-deps
run "$HERE"

# Bake the target env's absolute interpreter into a gitignored `.runtime-env`
# sidecar so the hub supervisor can respawn this service under systemd's
# minimal PATH (no `mamba`).
PYBIN="$(mamba run -n "$ENV" python -c 'import sys; print(sys.executable)')"
printf 'AWM_PYTHON=%s\nAWM_ENV_BIN=%s\n' "$PYBIN" "$(dirname "$PYBIN")" \
    > "$HERE/.runtime-env"

echo "Installed awm-httpsfront into env '$ENV'."
