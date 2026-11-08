#!/usr/bin/env bash
# Build the claude-science plugin's own runtime: the conda env awm-claude-science, or AWM_ENV.
#
# The workbench binary is not built here. Upstream ships a self-contained binary whose
# `update` subcommand downloads, verifies and swaps it, so this script installs it only
# when missing and otherwise reports what it found. The ~6 GB conda/R environments in
# ~/.claude-science are the daemon's business on first run; `awm claude-science status`
# reports whether that has happened.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
AWM="${AWM_ROOT:-$(git -C "$HERE" rev-parse --show-toplevel)/awm}"
ENV="${AWM_ENV:-awm-claude-science}"

# The release this service has been verified against. Bumping it is a deliberate
# step: the binary self-updates by default, so this is a floor and a record, not
# a lock.
PINNED_VERSION="${CLAUDE_SCIENCE_VERSION:-0.1.27}"
BIN="${CLAUDE_SCIENCE_BIN:-$HOME/.local/bin/claude-science}"
INSTALLER_URL="https://claude.ai/install-claude-science.sh"

. "$AWM/plugin-env.sh"

run "$AWM/service_components/config" --no-deps
run "$AWM/service_components/gatewayclient" --no-deps
run "$HERE"

bake_runtime_env

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
