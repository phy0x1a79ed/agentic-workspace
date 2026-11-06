#!/usr/bin/env bash
# Canonical install for the awm-scopes service (editable, into the `awm` env).
#
# Installs the component libraries it imports (config, persistence) first, then
# the service itself. Components have no install.sh of their own — they are
# pulled in here as plain editable dependencies. Override the target env with
# AWM_ENV=<name>.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
AWM="${AWM_ROOT:-$(git -C "$HERE" rev-parse --show-toplevel)/awm}"
ENV="${AWM_ENV:-awm}"
run() { echo "+ pip install -e $*"; mamba run -n "$ENV" pip install -e "$@"; }

run "$AWM/service_components/config" --no-deps
run "$AWM/service_components/persistence" --no-deps
run "$AWM/service_components/gatewayclient" --no-deps
# This service embeds + queries vectors, so it also needs persistence's
# `search` extra (sentence-transformers, on the CPU torch wheel).
# Opt-in on purpose — see that script for why it is not a declared dependency.
# AWM_SEARCH=0 skips it (a small public host): FTS keeps working, semantic
# search reports the typed "search extra not installed" error.
if [[ "${AWM_SEARCH:-1}" != "0" ]]; then
    bash "$AWM/service_components/persistence/install-search.sh" "$ENV"
fi
# Provisioning imports the DVC wiring from awm.dvcdata.data_dvc.
run "$AWM/service_components/dvcdata" --no-deps
run "$HERE"

# Bake the target env's absolute interpreter into a gitignored `.runtime-env`
# sidecar so the hub supervisor can respawn this service under systemd's
# minimal PATH (no `mamba`).
PYBIN="$(mamba run -n "$ENV" python -c 'import sys; print(sys.executable)')"
printf 'AWM_PYTHON=%s\nAWM_ENV_BIN=%s\n' "$PYBIN" "$(dirname "$PYBIN")" \
    > "$HERE/.runtime-env"

echo "Installed awm-scopes into env '$ENV'."
