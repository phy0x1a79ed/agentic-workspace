#!/usr/bin/env bash
# Canonical install for the awm-penpot service (editable, into the `awm` env).
#
# Simpler than Trilium's install.sh: there is no server bundle to build or
# download here. The thing this service supervises is a docker-compose
# project that lives in its own separate repo (`projects/penpot/dev`) and is
# built by that repo's own `docker/images/build.sh` — this service only
# drives `docker compose` against whatever images are already tagged there.
# Provisioning Docker itself, and building the Penpot images, are both out of
# scope for this script; see INSTALL.md.
#
# Override the target env with AWM_ENV=<name>.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ENV="${AWM_ENV:-awm}"

REPO="$(git -C "$HERE" rev-parse --show-toplevel)"

run() { echo "+ pip install -e $*"; mamba run -n "$ENV" pip install -e "$@"; }

run "$REPO/awm/service_components/config" --no-deps
run "$REPO/awm/service_components/gatewayclient" --no-deps
run "$REPO/awm/services/penpot"

# Bake the target env's absolute interpreter into a gitignored `.runtime-env`
# sidecar so the hub supervisor can respawn this service under systemd's
# minimal PATH (no `mamba`) — same convention every awm service's run.sh reads.
PYBIN="$(mamba run -n "$ENV" python -c 'import sys; print(sys.executable)')"
printf 'AWM_PYTHON=%s\nAWM_ENV_BIN=%s\n' "$PYBIN" "$(dirname "$PYBIN")" \
    > "$HERE/.runtime-env"

if command -v docker >/dev/null 2>&1; then
    echo "docker found: $(docker --version)"
else
    echo "warning: no 'docker' on PATH — the service will register and" >&2
    echo "  report this via 'awm penpot status' rather than fail install." >&2
fi

echo "Installed awm-penpot into env '$ENV'."
