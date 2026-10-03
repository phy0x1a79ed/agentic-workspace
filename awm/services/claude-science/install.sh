#!/usr/bin/env bash
# Canonical install for the awm-claude-science service (editable, into the `awm`
# env), plus a check that the workbench binary this service supervises is there.
#
# Two halves, because this service has two kinds of dependency:
#
#   1. Python — the adapter, the supervisor, the mesh fronts and the MCP bridge.
#      Installs the component libraries it imports first, then httpsfront (the
#      fronts are a *configuration* of that component, not a copy of it), then
#      the service.
#
#   2. The workbench binary — NOT built here. Upstream ships a single
#      self-contained binary with its own installer, and its `update` subcommand
#      already downloads, sha256-verifies and atomically swaps, with `--to
#      <version>` for pinning and rollback. Reimplementing any of that would be
#      strictly worse. So this script installs it if missing and otherwise
#      leaves it alone, reporting what it found.
#
# Deliberately NOT here: provisioning the ~6 GB conda/R environments in
# ~/.claude-science. The daemon does that on first run, in the background, and
# it is the daemon's business. `awm claude-science status` reports whether it
# has happened, so a half-set-up node explains itself.
#
# Override the target env with AWM_ENV=<name>.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
WS="$(git -C "$HERE" rev-parse --show-toplevel)"
ENV="${AWM_ENV:-awm}"

# The release this service has been verified against. Bumping it is a deliberate
# step: the binary self-updates by default, so this is a floor and a record, not
# a lock.
PINNED_VERSION="${CLAUDE_SCIENCE_VERSION:-0.1.27}"
BIN="${CLAUDE_SCIENCE_BIN:-$HOME/.local/bin/claude-science}"
INSTALLER_URL="https://claude.ai/install-claude-science.sh"

run() { echo "+ pip install -e $*"; mamba run -n "$ENV" pip install -e "$@"; }

run "$WS/awm/service_components/config" --no-deps
run "$WS/awm/service_components/gatewayclient" --no-deps
run "$WS/awm/services/httpsfront" --no-deps
run "$WS/awm/services/claude-science"

# Bake the target env's absolute interpreter into a gitignored `.runtime-env`
# sidecar so the hub supervisor can respawn this service under systemd's
# minimal PATH (no `mamba`).
PYBIN="$(mamba run -n "$ENV" python -c 'import sys; print(sys.executable)')"
printf 'AWM_PYTHON=%s\nAWM_ENV_BIN=%s\n' "$PYBIN" "$(dirname "$PYBIN")" \
    > "$HERE/.runtime-env"

# -- the workbench binary ---------------------------------------------------
if [ "${CLAUDE_SCIENCE_SKIP_BINARY:-}" = "1" ]; then
    echo "Skipping the workbench binary (CLAUDE_SCIENCE_SKIP_BINARY=1)."
elif [ -x "$BIN" ]; then
    HAVE="$("$BIN" --version 2>/dev/null | awk '{print $2}')"
    echo "Workbench binary present: $BIN (${HAVE:-unknown}; verified against $PINNED_VERSION)."
    if [ -n "$HAVE" ] && [ "$HAVE" != "$PINNED_VERSION" ]; then
        echo "  note: not the pinned build. It self-updates by default; pin or roll"
        echo "        back with: claude-science update --to $PINNED_VERSION"
    fi
else
    # The sandbox is not optional — the daemon refuses to start rather than run
    # code unsandboxed — so a missing bwrap is a failure to surface now, not at
    # first launch. bubblewrap must be >= 0.8.0.
    MISSING=""
    for tool in bwrap socat; do
        command -v "$tool" >/dev/null 2>&1 || MISSING="$MISSING $tool"
    done
    if [ -n "$MISSING" ]; then
        echo "FATAL: the workbench sandbox needs:$MISSING" >&2
        echo "       sudo apt-get install -y bubblewrap socat  (bwrap >= 0.8.0)" >&2
        exit 1
    fi
    echo "Installing the workbench binary from $INSTALLER_URL …"
    curl -fsSL "$INSTALLER_URL" | bash
    [ -x "$BIN" ] || { echo "FATAL: installer did not produce $BIN" >&2; exit 1; }
    echo "Installed $("$BIN" --version 2>/dev/null || echo claude-science)."
    echo "First launch provisions ~6 GB of Python/R environments in the"
    echo "background; the UI is usable well before that finishes."
fi

echo "Installed awm-claude-science into env '$ENV'."
