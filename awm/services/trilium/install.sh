#!/usr/bin/env bash
# Canonical install for the awm-trilium service (editable, into the `awm` env),
# plus the Trilium server bundle it supervises.
#
# Three halves, because this service has three kinds of dependency:
#
#   1. Python — the adapter, the per-user supervisor and the per-user mesh
#      fronts. Installs the component libraries it imports first, then
#      httpsfront (a front is a *configuration* of that component, not a copy
#      of it), then the service.
#
#   2. The server bundle. Two ways to get one, and the choice is the whole
#      difference between a build node and a serving node:
#
#        a. Build the fork. The `release` worktree of `projects/trilium` *is*
#           the runnable server, so every line we change is tracked TypeScript
#           on a branch rather than an edit to a build artifact. Expensive, so
#           it is stamped and skipped when the tree has not moved.
#        b. Download the published server tarball for the pinned tag. Upstream
#           ships a Node runtime inside it, so this path needs no toolchain at
#           all — which is what lets sirius install in a minute instead of
#           building TypeScript on two vCPUs for an hour.
#
#   3. A node toolchain, for (a) only. Its own mamba env pinned to the version
#      upstream's `.nvmrc` names.
#
# Which one runs is decided by TRILIUM_INSTALL_MODE, and by default by whether
# a fork exists. Every step is idempotent and skips itself when already
# satisfied, because this script runs on every deploy (awm/gateway/install.sh
# invokes each service's).
#
# Override the target env with AWM_ENV=<name>.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ENV="${AWM_ENV:-awm}"

# Two different roots, and they coincide only in the deployed tree. REPO is the
# awm checkout this script belongs to. WS is the workspace holding `projects/`
# and `.awm/`. Run from a scope worktree they differ, and using the git toplevel
# for a `projects/` path silently addresses a directory that does not exist.
REPO="$(git -C "$HERE" rev-parse --show-toplevel)"
workspace_root() {
    [ -n "${AWM_WORKSPACE:-}" ] && { printf '%s\n' "$AWM_WORKSPACE"; return; }
    local d="$1"
    while [ "$d" != "/" ]; do
        if [ -d "$d/projects" ] && [ -f "$d/AGENTS.md" ]; then
            printf '%s\n' "$d"
            return
        fi
        d="$(dirname "$d")"
    done
    printf '%s\n' "$REPO"          # standalone checkout: nothing above it
}
WS="$(workspace_root "$HERE")"

NODE_ENV_NAME="${TRILIUM_NODE_ENV:-trilium}"
STATE_DIR="${TRILIUM_STATE_DIR:-$WS/.awm/services/trilium}"
# The fork worktree this node serves. `release` by default. A dev sandbox
# points TRILIUM_FORK_DIR at `dev`.
FORK_DIR="${TRILIUM_FORK_DIR:-$WS/projects/trilium/release}"
FORK_ENTRY="$FORK_DIR/apps/server/dist/main.cjs"

# The tag the tarball path downloads. Keep in step with bootstrap-fork.sh:
# a node serving the tarball and a node serving the fork must serve the same
# version, or a note written on one is a note the other cannot open.
BASE_REF="${TRILIUM_BASE_REF:-v0.105.0}"
TARBALL_DIR="$STATE_DIR/server"
TARBALL_ENTRY="$TARBALL_DIR/main.cjs"

# auto | build | tarball. `auto` builds when a fork is checked out and falls
# back to the tarball when there is none.
MODE="${TRILIUM_INSTALL_MODE:-auto}"

run() { echo "+ pip install -e $*"; mamba run -n "$ENV" pip install -e "$@"; }

run "$REPO/awm/service_components/config" --no-deps
run "$REPO/awm/service_components/gatewayclient" --no-deps
run "$REPO/awm/services/httpsfront" --no-deps
run "$REPO/awm/services/trilium"

# Bake the target env's absolute interpreter into a gitignored `.runtime-env`
# sidecar so the hub supervisor can respawn this service under systemd's
# minimal PATH (no `mamba`).
PYBIN="$(mamba run -n "$ENV" python -c 'import sys; print(sys.executable)')"
printf 'AWM_PYTHON=%s\nAWM_ENV_BIN=%s\n' "$PYBIN" "$(dirname "$PYBIN")" \
    > "$HERE/.runtime-env"

mkdir -p "$STATE_DIR"

if [ "$MODE" = "auto" ]; then
    if [ -e "$FORK_DIR/.git" ]; then MODE=build; else MODE=tarball; fi
fi

# -- (b) the published tarball ----------------------------------------------
if [ "$MODE" = "tarball" ]; then
    case "$(uname -m)" in
        x86_64)  TARCH=x64 ;;
        aarch64) TARCH=arm64 ;;
        *) echo "error: no published Trilium server tarball for $(uname -m)." >&2
           echo "  build the fork instead: TRILIUM_INSTALL_MODE=build" >&2
           exit 1 ;;
    esac
    ASSET="TriliumNotes-Server-${BASE_REF}-linux-${TARCH}.tar.xz"
    STAMP_FILE="$STATE_DIR/tarball-stamp"
    if [ "$(cat "$STAMP_FILE" 2>/dev/null || true)" = "$ASSET" ] \
       && [ -f "$TARBALL_ENTRY" ]; then
        echo "Trilium server $BASE_REF ($TARCH) already unpacked — nothing to do."
    else
        TMP="$(mktemp -d)"
        trap 'rm -rf "$TMP"' EXIT
        echo "Downloading $ASSET …"
        if command -v gh >/dev/null 2>&1; then
            gh release download "$BASE_REF" --repo TriliumNext/Trilium \
                --pattern "$ASSET" --dir "$TMP"
        else
            curl -fsSL -o "$TMP/$ASSET" \
                "https://github.com/TriliumNext/Trilium/releases/download/$BASE_REF/$ASSET"
        fi
        # The release carries no checksum file, so integrity rests on the TLS
        # transport and on gh's own verification of the release. Record what was
        # unpacked so a swapped artifact is at least visible after the fact.
        sha256sum "$TMP/$ASSET" | cut -d' ' -f1 > "$STATE_DIR/tarball-sha256"
        echo "Unpacking into $TARBALL_DIR …"
        rm -rf "$TARBALL_DIR"
        mkdir -p "$TARBALL_DIR"
        # The archive holds a single top-level directory named after the asset.
        tar -xJf "$TMP/$ASSET" -C "$TARBALL_DIR" --strip-components=1
        test -f "$TARBALL_ENTRY" || {
            echo "error: $ASSET did not contain main.cjs at its root." >&2
            exit 1
        }
        printf '%s\n' "$ASSET" > "$STAMP_FILE"
        rm -rf "$TMP"
        trap - EXIT
    fi
    echo "Installed awm-trilium into env '$ENV'."
    echo "Server:  $TARBALL_ENTRY (published $BASE_REF, bundled node runtime)"
    echo "State:   $STATE_DIR"
    exit 0
fi

# -- (a) build the fork ------------------------------------------------------
#
# A deploy must never clone the monorepo behind your back, so an absent fork is
# reported rather than fixed. It is not fatal, though: the gateway's install.sh
# runs every service's under `set -e`, so exiting here would abort the whole
# deploy on a node that has no fork.
#
# TRILIUM_REQUIRE_SERVER=1 is for a node that is *supposed* to serve it, where a
# warning would be the wrong answer.
if [ ! -e "$FORK_DIR/.git" ]; then
    echo "warning: no Trilium fork at $FORK_DIR — trilium will register unbuilt." >&2
    echo "  run $HERE/bootstrap-fork.sh to create it," >&2
    echo "  or set TRILIUM_INSTALL_MODE=tarball to serve the published build." >&2
    if [ "${TRILIUM_REQUIRE_SERVER:-}" = "1" ]; then
        exit 1
    fi
    echo "Installed awm-trilium into env '$ENV' (no server)."
    exit 0
fi

# The node version upstream pins, so the build runs under the toolchain the
# lockfile was resolved against.
NODE_VERSION="$(tr -d ' \n' < "$FORK_DIR/.nvmrc")"
if mamba run -n "$NODE_ENV_NAME" node --version >/dev/null 2>&1; then
    echo "node env '$NODE_ENV_NAME' present: $(mamba run -n "$NODE_ENV_NAME" node --version)."
else
    echo "Creating node env '$NODE_ENV_NAME' (nodejs $NODE_VERSION) …"
    mamba create -y -n "$NODE_ENV_NAME" -c conda-forge "nodejs==$NODE_VERSION"
fi

NODE_BIN="$(dirname "$(mamba run -n "$NODE_ENV_NAME" node -p 'process.execPath')")"
# The supervisor respawns under systemd's minimal PATH, where neither `node` nor
# the `mamba` that could find it exists. Recording the absolute directory here
# is what lets it build a working PATH for the child.
printf '%s\n' "$NODE_BIN" > "$STATE_DIR/node-bin"

# pnpm because the workspace declares `packageManager: pnpm@11.22.0` and pins a
# tree of ~90 projects with an overrides block that only pnpm honours. Whatever
# pnpm we install here is a bootstrap: it reads that field and hands off to the
# pinned version, so the toolchain the build runs under is upstream's choice.
command -v "$NODE_BIN/pnpm" >/dev/null 2>&1 || \
    ( echo "Installing pnpm into env '$NODE_ENV_NAME' …"; \
      PATH="$NODE_BIN:$PATH" "$NODE_BIN/npm" i -g --no-fund --no-audit pnpm )

# What a build corresponds to: the commit, whether the tree was clean, and the
# lockfile the install resolved. Any of the three moving means the built
# artifacts no longer describe the source, which is the whole point of stamping
# rather than testing for the presence of a file.
STAMP_DIR="$FORK_DIR/.awm"          # already excluded by the bare repo
STAMP="$STAMP_DIR/trilium-build-stamp"
HEAD_SHA="$(git -C "$FORK_DIR" rev-parse HEAD)"
DIRTY=0
[ -z "$(git -C "$FORK_DIR" status --porcelain)" ] || DIRTY=1
LOCK_SHA="$(sha256sum "$FORK_DIR/pnpm-lock.yaml" | cut -d' ' -f1)"
WANT="head=$HEAD_SHA dirty=$DIRTY lock=$LOCK_SHA"

if [ "${TRILIUM_SKIP_BUILD:-}" = "1" ]; then
    echo "TRILIUM_SKIP_BUILD=1 — leaving $FORK_DIR as it is."
elif [ "$(cat "$STAMP" 2>/dev/null || true)" = "$WANT" ] && [ -f "$FORK_ENTRY" ]; then
    echo "Server built at $(git -C "$FORK_DIR" describe --tags --always) — nothing to do."
else
    echo "Building the Trilium server in $FORK_DIR (several minutes) …"
    # CI=true keeps the workspace's postinstall on its non-interactive path and
    # keeps pnpm from writing progress frames into a deploy log. Only that exact
    # string is tested for by scripts that check it.
    ( cd "$FORK_DIR" && CI=true PATH="$NODE_BIN:$PATH" \
        "$NODE_BIN/pnpm" install --frozen-lockfile )
    # Only the server. `pnpm build` at the root would also build the Electron
    # desktop app and the docs site, neither of which anything here serves.
    ( cd "$FORK_DIR" && CI=true PATH="$NODE_BIN:$PATH" \
        "$NODE_BIN/pnpm" server:build )
    test -f "$FORK_ENTRY" || {
        echo "error: the build produced no bundle at $FORK_ENTRY." >&2
        exit 1
    }
    mkdir -p "$STAMP_DIR"
    printf '%s\n' "$WANT" > "$STAMP"
fi

echo "Installed awm-trilium into env '$ENV'."
echo "Server:  $FORK_ENTRY ($(git -C "$FORK_DIR" describe --tags --always --dirty))"
echo "State:   $STATE_DIR"
