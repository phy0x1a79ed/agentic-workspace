#!/usr/bin/env bash
# First setup of a node from this checkout: the Python env, the plugins, the Rust core, and the
# gateway's state directory. AWM_BIN names a built awm binary; without one this script builds
# it in the Rust container (scripts/rust-build.sh).
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
WORKSPACE_ROOT="$(git -C "$HERE" rev-parse --show-toplevel)"
if [ -n "${AWM_WORKSPACE:-}" ] && [ "$(cd "$AWM_WORKSPACE" 2>/dev/null && pwd -P)" != "$(cd "$WORKSPACE_ROOT" && pwd -P)" ]; then
    echo "refusing: AWM_WORKSPACE=$AWM_WORKSPACE but this checkout is $WORKSPACE_ROOT" >&2
    exit 1
fi

if ! mamba env list 2>/dev/null | awk '{print $1}' | grep -qx "awm"; then
    mamba env create -f "$HERE/environment.yml"
else
    mamba env update -f "$HERE/environment.yml" --prune
fi

if [ -z "${AWM_BIN:-}" ]; then
    "$WORKSPACE_ROOT/scripts/rust-build.sh" build --release --target x86_64-unknown-linux-musl -p awm-cli
    AWM_BIN="$("$WORKSPACE_ROOT/scripts/rust-build.sh" --target-dir)/x86_64-unknown-linux-musl/release/awm"
fi
AWM_ENV=awm AWM_BIN="$AWM_BIN" bash "$HERE/install.sh"

"$HOME/.local/bin/awm" bootstrap --state "$WORKSPACE_ROOT/.awm" --awm-root "$WORKSPACE_ROOT"
mkdir -p "$HOME/.config/systemd/user"
cp "$WORKSPACE_ROOT/core/deploy/awm.service" "$HOME/.config/systemd/user/awm.service"
systemctl --user daemon-reload
echo "Done. Start the gateway with: systemctl --user enable --now awm"
