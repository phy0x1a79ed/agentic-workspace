#!/usr/bin/env bash
# Canonical install for the awm-social service (editable, into the `awm` env).
#
# Installs the component libraries it imports (config, persistence,
# gatewayclient, kept, trustpolicy) first, then the service itself (which pulls in discord.py and
# slack_sdk). Components have no install.sh of their own — they are pulled in
# here as plain editable dependencies. Override the target env with AWM_ENV=<name>.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
AWM="${AWM_ROOT:-$(git -C "$HERE" rev-parse --show-toplevel)/awm}"
ENV="${AWM_ENV:-awm}"
run() { echo "+ pip install -e $*"; mamba run -n "$ENV" pip install -e "$@"; }

run "$AWM/service_components/config" --no-deps
run "$AWM/service_components/persistence" --no-deps
run "$AWM/service_components/gatewayclient" --no-deps
run "$AWM/service_components/kept" --no-deps
run "$AWM/service_components/grantlib" --no-deps
run "$AWM/service_components/trustpolicy" --no-deps
run "$HERE"
mamba run -n "$ENV" python -c 'import slack_sdk' \
    || { echo "slack_sdk did not install into env '$ENV'" >&2; exit 1; }

# Bake the target env's absolute interpreter into a gitignored `.runtime-env`
# sidecar so the hub supervisor can respawn this service under systemd's
# minimal PATH (no `mamba`).
PYBIN="$(mamba run -n "$ENV" python -c 'import sys; print(sys.executable)')"
printf 'AWM_PYTHON=%s\nAWM_ENV_BIN=%s\n' "$PYBIN" "$(dirname "$PYBIN")" \
    > "$HERE/.runtime-env"

echo "Installed awm-social into env '$ENV'."
