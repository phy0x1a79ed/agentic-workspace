#!/usr/bin/env bash
# Canonical install for the awm-dsh service (editable, into the `awm` env), plus
# the node runtime the DeepSeek Harness itself needs.
#
# Three halves, because this service has three kinds of dependency:
#
#   1. Python — the adapter, the supervisor and the mesh front. Installs the
#      component libraries it imports first, then httpsfront (the front is a
#      *configuration* of that component, not a copy of it), then the service.
#
#   2. A node toolchain — its own mamba env pinned to node 24. The harness needs
#      >= 22.19 and the node on the host PATH is older and carries no npm, so
#      borrowing it is not an option.
#
#   3. The harness — built from a fork held as its own awm project. The
#      `release` worktree of `projects/deepseek-harness` *is* the runnable
#      harness, so every line we change to it is tracked TypeScript on a branch
#      rather than an edit to a build artifact. Building is the expensive step,
#      so it is stamped and skipped when the tree has not moved.
#
# Every step is idempotent and skips itself when already satisfied, because this
# script runs on every deploy (awm/gateway/install.sh invokes each service's).
#
# Override the target env with AWM_ENV=<name>.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
WS="$(git -C "$HERE" rev-parse --show-toplevel)"
ENV="${AWM_ENV:-awm}"

NODE_ENV_NAME="${DSH_NODE_ENV:-dsh}"
STATE_DIR="${DSH_STATE_DIR:-$WS/.awm/services/dsh}"
# The harness fork worktree this node serves. `release` by default; a dev
# sandbox points DSH_FORK_DIR at `dev`.
FORK_DIR="${DSH_FORK_DIR:-$WS/projects/deepseek-harness/release}"

run() { echo "+ pip install -e $*"; mamba run -n "$ENV" pip install -e "$@"; }

run "$WS/awm/service_components/config" --no-deps
run "$WS/awm/service_components/gatewayclient" --no-deps
run "$WS/awm/services/httpsfront" --no-deps
run "$WS/awm/services/dsh"

# Bake the target env's absolute interpreter into a gitignored `.runtime-env`
# sidecar so the hub supervisor can respawn this service under systemd's
# minimal PATH (no `mamba`).
PYBIN="$(mamba run -n "$ENV" python -c 'import sys; print(sys.executable)')"
printf 'AWM_PYTHON=%s\nAWM_ENV_BIN=%s\n' "$PYBIN" "$(dirname "$PYBIN")" \
    > "$HERE/.runtime-env"

# -- the node toolchain -----------------------------------------------------
if mamba run -n "$NODE_ENV_NAME" node --version >/dev/null 2>&1; then
    echo "node env '$NODE_ENV_NAME' present: $(mamba run -n "$NODE_ENV_NAME" node --version)."
else
    echo "Creating node env '$NODE_ENV_NAME' (nodejs 24) …"
    mamba create -y -n "$NODE_ENV_NAME" -c conda-forge 'nodejs>=24,<25'
fi

NODE_BIN="$(dirname "$(mamba run -n "$NODE_ENV_NAME" node -p 'process.execPath')")"
mkdir -p "$STATE_DIR"
# The supervisor respawns under systemd's minimal PATH, where neither `node` nor
# the `mamba` that could find it exists. Recording the absolute directory here
# is what lets it build a working PATH for the child.
printf '%s\n' "$NODE_BIN" > "$STATE_DIR/node-bin"

# -- the harness -----------------------------------------------------------
# pnpm because the harness declares `packageManager: pnpm@11.7.0` and is a pnpm
# workspace of ~250 projects; npm 11's peer resolver does not converge on this
# tree in any usable time. Whatever pnpm we install here is a bootstrap: it
# reads that field and hands off to the pinned version, so the toolchain the
# build runs under is upstream's choice, not this env's.
#
# The workspace's own pnpm-workspace.yaml carries the install settings, so
# nothing here generates one. Its `allowBuilds` already names node-pty and
# koffi — without them the GUI still loads and every tool that touches a
# subprocess fails at use time.
command -v "$NODE_BIN/pnpm" >/dev/null 2>&1 || \
    ( echo "Installing pnpm into env '$NODE_ENV_NAME' …"; \
      PATH="$NODE_BIN:$PATH" "$NODE_BIN/npm" i -g --no-fund --no-audit pnpm )

# A deploy must never clone 109 MB behind your back, so an absent fork is
# reported rather than fixed. It is not fatal, though: the gateway's install.sh
# runs every service's under `set -e`, so exiting here would abort the whole
# deploy on a node that has no fork — and a node without one is a node that
# simply does not serve the harness. The service still registers and reports it
# through `status`, the same shape as an unbuilt fork.
#
# DSH_REQUIRE_HARNESS=1 is for a node that is *supposed* to serve it, where a
# warning would be the wrong answer.
if [ ! -e "$FORK_DIR/.git" ]; then
    echo "warning: no harness fork at $FORK_DIR — dsh will register unbuilt." >&2
    echo "  awm project create --name deepseek-harness \\" >&2
    echo "      --fork-url https://github.com/deepseek-ai/deepseek-harness" >&2
    echo "  then create the 'dev' and 'release' scopes from tag dsh-v0.1.1-rc.2." >&2
    if [ "${DSH_REQUIRE_HARNESS:-}" = "1" ]; then
        exit 1
    fi
    echo "Installed awm-dsh into env '$ENV' (no harness)."
    exit 0
fi

# What a build corresponds to: the commit, whether the tree was clean, and the
# lockfile the install resolved. Any of the three moving means the built
# artifacts no longer describe the source, which is the whole point of stamping
# rather than testing for the presence of a file.
STAMP_DIR="$FORK_DIR/.awm"          # already excluded by the bare repo
STAMP="$STAMP_DIR/dsh-build-stamp"
HEAD_SHA="$(git -C "$FORK_DIR" rev-parse HEAD)"
DIRTY=0
[ -z "$(git -C "$FORK_DIR" status --porcelain)" ] || DIRTY=1
LOCK_SHA="$(sha256sum "$FORK_DIR/pnpm-lock.yaml" | cut -d' ' -f1)"
WANT="head=$HEAD_SHA dirty=$DIRTY lock=$LOCK_SHA"

if [ "${DSH_SKIP_BUILD:-}" = "1" ]; then
    echo "DSH_SKIP_BUILD=1 — leaving $FORK_DIR as it is."
elif [ "$(cat "$STAMP" 2>/dev/null || true)" = "$WANT" ] \
     && [ -f "$FORK_DIR/apps/cli/lib/bin.js" ]; then
    echo "Harness built at $(git -C "$FORK_DIR" describe --tags --always) — nothing to do."
else
    echo "Building the harness in $FORK_DIR (a few minutes) …"
    # CI=true is load-bearing twice over: the root postinstall runs
    # scripts/install-lefthook.mjs, which cannot enable extensions.worktreeConfig
    # against a bare repo's config and fails the whole install — and it returns
    # early on exactly this string. Git hooks have no business in a supervised
    # worktree anyway. `1` does not match; the script tests for "true".
    ( cd "$FORK_DIR" && CI=true PATH="$NODE_BIN:$PATH" \
        "$NODE_BIN/pnpm" install --frozen-lockfile )
    ( cd "$FORK_DIR" && CI=true PATH="$NODE_BIN:$PATH" \
        "$NODE_BIN/pnpm" run build )
    mkdir -p "$STAMP_DIR"
    printf '%s\n' "$WANT" > "$STAMP"
fi

echo "Installed awm-dsh into env '$ENV'."
echo "Harness:  $FORK_DIR ($(git -C "$FORK_DIR" describe --tags --always --dirty))"
echo "DSH_HOME: $STATE_DIR/home"
