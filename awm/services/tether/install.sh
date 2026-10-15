#!/usr/bin/env bash
# Canonical install for the awm-tether service.
#
# The service is the gateway adapter and installs like every other service. The
# binaries it supervises are not built here: they are built and published by the
# phy0x1a79ed/tether repository, and this host fetches the version RELEASE pins
# with fetch-release.sh, which is the same mechanism run.sh uses on every start.
# A host that cannot reach the release still installs cleanly and reports the
# missing binaries through `awm tether status` rather than failing here.
#
# Override the target env with AWM_ENV=<name>.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
WS="$(git -C "$HERE" rev-parse --show-toplevel)"
ENV="${AWM_ENV:-awm}"
run() { echo "+ pip install -e $*"; mamba run -n "$ENV" pip install -e "$@"; }

run "$WS/awm/service_components/gatewayclient" --no-deps
run "$WS/awm/services/tether"

# Bake the target env's absolute interpreter into a gitignored `.runtime-env`
# sidecar so the hub supervisor can respawn this service under systemd's
# minimal PATH (no `mamba`).
PYBIN="$(mamba run -n "$ENV" python -c 'import sys; print(sys.executable)')"
printf 'AWM_PYTHON=%s\nAWM_ENV_BIN=%s\n' "$PYBIN" "$(dirname "$PYBIN")" \
    > "$HERE/.runtime-env"

# Fetch this host's pinned binaries now, so a fresh install comes up running
# rather than waiting for the first restart. Non-fatal: run.sh fetches again on
# every start, and a host offline now catches up then.
bash "$HERE/fetch-release.sh" || echo "fetch-release.sh failed; see .awm/services/tether/fetch.log" >&2

echo "Installed awm-tether into env '$ENV'."
