#!/usr/bin/env bash
# Canonical install for the awm-hermes service (editable, into the `awm` env),
# plus a check that the Hermes Agent runtime this service supervises is there.
#
# Two halves, because this service has two kinds of dependency:
#
#   1. Python — the adapter, the supervisor and the mesh front. Installs the
#      component libraries it imports first, then httpsfront (the front is a
#      *configuration* of that component, not a copy of it), then the service.
#
#   2. The Hermes Agent runtime — NOT built here. Upstream ships an installer
#      that clones its own checkout into $HERMES_HOME/hermes-agent, provisions a
#      Python 3.11 venv and a managed Node, and links a launcher into
#      ~/.local/bin. Its `hermes update` subcommand then owns updates in place.
#      Reimplementing any of that would be strictly worse. So this script
#      reports what it found and tells you how to get it, rather than installing
#      it behind your back: the install is ~2 GB and takes several minutes.
#
# Deliberately NOT here: writing config.yaml, the API key, the MCP catalog or
# SOUL.md. Those are the operator's, and `hermes doctor` reports on them.
#
# Override the target env with AWM_ENV=<name>.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
WS="$(git -C "$HERE" rev-parse --show-toplevel)"
ENV="${AWM_ENV:-awm}"

HERMES_BIN="${HERMES_BIN:-$HOME/.local/bin/hermes}"
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
INSTALLER_URL="https://raw.githubusercontent.com/NousResearch/hermes-agent/main/scripts/install.sh"

run() { echo "+ pip install -e $*"; mamba run -n "$ENV" pip install -e "$@"; }

run "$WS/awm/service_components/config" --no-deps
run "$WS/awm/service_components/gatewayclient" --no-deps
run "$WS/awm/services/httpsfront" --no-deps
run "$WS/awm/services/hermes"

# Bake the target env's absolute interpreter into a gitignored `.runtime-env`
# sidecar so the hub supervisor can respawn this service under systemd's
# minimal PATH (no `mamba`).
PYBIN="$(mamba run -n "$ENV" python -c 'import sys; print(sys.executable)')"
printf 'AWM_PYTHON=%s\nAWM_ENV_BIN=%s\n' "$PYBIN" "$(dirname "$PYBIN")" \
    > "$HERE/.runtime-env"

# -- the Hermes Agent runtime -----------------------------------------------
if [ -x "$HERMES_BIN" ]; then
    echo "Hermes runtime present: $HERMES_BIN"
    HERMES_HOME="$HERMES_HOME" "$HERMES_BIN" --version 2>/dev/null | sed 's/^/  /' || true
    if [ ! -d "$HERMES_HOME/hermes-agent/hermes_cli/web_dist" ]; then
        echo "  note: the dashboard SPA has not been built. The service starts the"
        echo "        dashboard with --skip-build, which triggers one recovery build"
        echo "        on first launch — slow, but it only happens once."
    fi
else
    echo "Hermes runtime NOT installed at $HERMES_BIN."
    echo "  Install it with upstream's own installer (~2 GB, several minutes):"
    echo "    curl -fsSL $INSTALLER_URL -o /tmp/hermes-install.sh"
    echo "    bash /tmp/hermes-install.sh --skip-browser --skip-computer-use \\"
    echo "         --skip-setup --non-interactive"
    echo "  awm already has rlm-browser and a compute surface, so the Playwright"
    echo "  and Computer-Use stages are dead weight here."
    echo "  Then configure it — see INSTALL.md § The runtime."
    echo "  The service will register either way and report this via status."
fi

echo "Installed awm-hermes into env '$ENV'."
