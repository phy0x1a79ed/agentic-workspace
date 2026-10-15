#!/usr/bin/env bash
# Canonical install for the awm-dvc service (editable, into the `awm` env).
#
# Installs the component libraries it imports (config, persistence, gatewayclient)
# and this service, then resolves the `globus` CLI from its OWN env and bakes both that
# path and the target interpreter into a gitignored `.runtime-env` sidecar, so
# the supervisor can respawn under systemd's minimal PATH (no `mamba`).
#
# The globus CLI is deliberately NOT a dependency of the `awm` env — it is a
# large, separately-authenticated tool invoked as a subprocess. Its login state
# lives in its own env and is an operator step (see INSTALL.md).
#
# Override the target env with AWM_ENV=<name>, the globus env with GLOBUS_ENV=<name>.
set -euo pipefail
cd "$(dirname "$0")"
WS="$(git rev-parse --show-toplevel)"
ENV="${AWM_ENV:-awm}"
GENV="${GLOBUS_ENV:-globus}"
run() { echo "+ pip install -e $*"; mamba run -n "$ENV" pip install -e "$@"; }

run "$WS/awm/service_components/config" --no-deps
run "$WS/awm/service_components/persistence" --no-deps
run "$WS/awm/service_components/gatewayclient" --no-deps
run "$WS/awm/services/dvc"

GBIN="$(mamba run -n "$GENV" python -c 'import shutil; print(shutil.which("globus") or "")' 2>/dev/null || true)"
if [ -z "$GBIN" ]; then
    echo "WARNING: no 'globus' CLI found in env '$GENV'." >&2
    echo "         Every verb that moves bytes will fail until it exists and is" >&2
    echo "         logged in. See INSTALL.md." >&2
fi

PYBIN="$(mamba run -n "$ENV" python -c 'import sys; print(sys.executable)')"

# The nightly used to be `awm-dvc-mirror`, and a systemd unit still pointing at
# that name must fail loudly rather than keep running a mirror that no longer
# exists. pip removes the old script on reinstall, but an env installed from an
# older tree can carry it, so make the removal explicit.
rm -f "$(dirname "$PYBIN")/awm-dvc-mirror"
printf 'AWM_PYTHON=%s\nAWM_ENV_BIN=%s\nAWM_DVC_GLOBUS_BIN=%s\n' \
    "$PYBIN" "$(dirname "$PYBIN")" "$GBIN" > ./.runtime-env

echo "Installed awm-dvc into env '$ENV' (globus CLI: ${GBIN:-NOT FOUND})."
