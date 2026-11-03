#!/usr/bin/env bash
# Canonical install for the awm-fileviewer service (editable, into the `awm` env).
#
# Installs the component libraries it imports (config, gatewayclient) first,
# then the service itself. The file-serving listener is pure stdlib
# (http.server, mimetypes, pathlib) — no third-party deps and no DB (so no
# awm-persistence). Override the target env with AWM_ENV=<name>.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
AWM="${AWM_ROOT:-$(git -C "$HERE" rev-parse --show-toplevel)/awm}"
ENV="${AWM_ENV:-awm}"
run() { echo "+ pip install -e $*"; mamba run -n "$ENV" pip install -e "$@"; }

run "$AWM/service_components/config" --no-deps
run "$AWM/service_components/gatewayclient" --no-deps
run "$HERE"

# Bake the target env's absolute interpreter into a gitignored `.runtime-env`
# sidecar so the hub supervisor can respawn this service under systemd's
# minimal PATH (no `mamba`).
PYBIN="$(mamba run -n "$ENV" python -c 'import sys; print(sys.executable)')"
printf 'AWM_PYTHON=%s\nAWM_ENV_BIN=%s\n' "$PYBIN" "$(dirname "$PYBIN")" \
    > "$HERE/.runtime-env"

echo "Installed awm-fileviewer into env '$ENV'."
