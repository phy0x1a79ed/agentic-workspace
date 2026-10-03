#!/usr/bin/env bash
# Take a trilium change from this worktree to the gateway running on this node.
#
# Three things have to move and `awm deploy` alone moves none of them:
#
#   1. The awm commits have to reach the tree the editable install resolves
#      `awm` to — the workspace-root checkout, on `release`. Until they do, the
#      running gateway imports the previous code and reports success.
#   2. The Trilium fork has to be rebuilt when its revision moved. `awm deploy`
#      re-runs a service's install.sh only when the *set* of installed dists
#      changes, so a rebuilt bundle never lands after the first deploy — the
#      same trap that leaves drawio serving a stale client patch.
#   3. The service has to be restarted to pick up any of it.
#
# What this does NOT do: push to GitHub, to capella's bare, or to mira. Those
# are fleet promotion, they are node-shape-specific, and getting them wrong
# ships something other than what was promoted. This script makes the change
# live *here*. See the release-promotion notes before taking it further.
#
#   deploy.sh [--no-promote] [--no-fork] [--full] [--dry-run]
#
#   --no-promote  the release tree already has the commits; skip the merge
#   --no-fork     do not merge the Trilium fork's dev into release
#   --full        `awm deploy` (whole gateway) instead of restarting one service
#   --dry-run     print every step, run none of them
set -euo pipefail

PROMOTE=1 FORK=1 FULL=0 DRY=0
for arg in "$@"; do
    case "$arg" in
        --no-promote) PROMOTE=0 ;;
        --no-fork)    FORK=0 ;;
        --full)       FULL=1 ;;
        --dry-run)    DRY=1 ;;
        -h|--help)    sed -n '2,22p' "$0"; exit 0 ;;
        *) echo "unknown option: $arg" >&2; exit 2 ;;
    esac
done

say() { printf '\n== %s\n' "$*"; }
run() {
    if [ "$DRY" = 1 ]; then printf '  would run: %s\n' "$*"; else printf '  + %s\n' "$*"; "$@"; fi
}

# The workspace is the directory holding both `projects/` and `AGENTS.md`. The
# same walk every awm bootstrap script uses, and for the same reason: this
# script runs from a scope worktree whose depth is not fixed.
workspace_root() {
    local d; d="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    while [ "$d" != "/" ]; do
        [ -d "$d/projects" ] && [ -f "$d/AGENTS.md" ] && { echo "$d"; return; }
        d="$(dirname "$d")"
    done
    echo "cannot find the workspace root above $0" >&2; exit 1
}

WS="$(workspace_root)"
SCOPE="$(git -C "$(dirname "${BASH_SOURCE[0]}")" rev-parse --show-toplevel)"
BRANCH="$(git -C "$SCOPE" rev-parse --abbrev-ref HEAD)"
BARE="$WS/projects/awm/.bare"
RELEASE="$WS"                      # the workspace-root checkout, on `release`
AWM_BIN="$(command -v awm || echo "$HOME/lib/miniforge3/envs/awm/bin/awm")"

echo "worktree: $SCOPE ($BRANCH)"
echo "release:  $RELEASE ($(git -C "$RELEASE" rev-parse --abbrev-ref HEAD) @ $(git -C "$RELEASE" rev-parse --short HEAD))"

# ---------------------------------------------------------------------------
# 1. Promote the awm commits into the release tree
# ---------------------------------------------------------------------------

if [ "$PROMOTE" = 1 ]; then
    say "promoting $BRANCH into release"

    if [ -n "$(git -C "$SCOPE" status --porcelain)" ]; then
        echo "refusing: $SCOPE has uncommitted changes. A deploy ships commits," >&2
        echo "and shipping while the tree is dirty makes the running code something" >&2
        echo "no commit describes. Commit or set the changes aside first." >&2
        exit 1
    fi

    if git -C "$BARE" merge-base --is-ancestor "$BRANCH" release 2>/dev/null; then
        echo "  release already contains $BRANCH — nothing to promote"
    else
        # CAUTION: the merge commit is made in a throwaway worktree of the bare,
        # never in the release checkout. That checkout is a deploy *target* —
        # it gets fetched and reset --hard — so a commit authored there is
        # discarded later with no warning. It only ever fast-forwards.
        HOLDER="$(git -C "$BARE" worktree list --porcelain \
                  | awk '/^worktree /{w=$2} /^branch refs\/heads\/release$/{print w}')"
        if [ -n "$HOLDER" ]; then
            echo "refusing: $BARE has 'release' checked out at $HOLDER, so a" >&2
            echo "throwaway worktree cannot take it. That is capella's shape, not" >&2
            echo "altair's — promote by hand there." >&2
            exit 1
        fi

        TMPWT="$WS/projects/awm/.promote-$$"
        run git -C "$BARE" worktree add --quiet "$TMPWT" release
        # `--no-ff` so the promotion is one reviewable commit even when the
        # branch happens to fast-forward. Which one it was is not something a
        # later bisect should have to reconstruct.
        if [ "$DRY" = 0 ]; then
            if ! git -C "$TMPWT" merge --no-ff -m "Merge $BRANCH into release: trilium" "$BRANCH"; then
                echo "merge conflict in $TMPWT — resolve it there, commit, then" >&2
                echo "re-run with --no-promote after removing the worktree." >&2
                exit 1
            fi
        fi
        run git -C "$BARE" worktree remove "$TMPWT"
    fi

    # Feed the release checkout from the *local* bare. Its `release` upstream
    # points at origin, which is fed from capella — a plain `git pull` here
    # takes a different ref than the promotion just wrote.
    run git -C "$RELEASE" fetch --quiet "$BARE" release
    run git -C "$RELEASE" merge --ff-only FETCH_HEAD
    echo "  release now at $(git -C "$RELEASE" rev-parse --short HEAD)"
    echo "  NOT pushed to GitHub, capella or mira — this node only."
fi

# ---------------------------------------------------------------------------
# 2. Promote the Trilium fork
# ---------------------------------------------------------------------------

FORK_DEV="$WS/projects/trilium/dev"
FORK_REL="$WS/projects/trilium/release"

# `-e`, not `-d`: a linked worktree's `.git` is a file pointing at the common
# git dir, so a directory test reports every worktree as not-a-repo.
if [ "$FORK" = 1 ] && [ -e "$FORK_DEV/.git" ] && [ -e "$FORK_REL/.git" ]; then
    say "promoting the Trilium fork"
    if git -C "$FORK_REL" merge-base --is-ancestor dev HEAD; then
        echo "  fork release already contains dev — nothing to promote"
    else
        run git -C "$FORK_REL" merge --no-ff -m "Merge dev into release" dev
    fi
fi

# ---------------------------------------------------------------------------
# 3. Install — unconditionally, because the deploy will not
# ---------------------------------------------------------------------------

say "installing the service from the release tree"
# Always, not conditionally. install.sh is stamped on the fork's HEAD, its dirty
# flag and its lockfile, so it is a few seconds when nothing moved and a rebuild
# when something did. `awm deploy` would skip it entirely: it keys on the set of
# installed dists, which a rebuilt bundle does not change.
run bash "$RELEASE/awm/services/trilium/install.sh"

# ---------------------------------------------------------------------------
# 4. Build the reception page — also unconditionally
# ---------------------------------------------------------------------------

say "building the reception page"
PAGE_DIR="$RELEASE/awm/pages/trilium"
VITE="$RELEASE/awm/node_modules/.bin/vite"
# Recorded before the build, because it is what decides whether the gateway has
# ever seen this page: mounts are discovered at boot, so a dist that did not
# exist at the last boot is not being served however fresh it is now.
PAGE_WAS_BUILT=0; [ -f "$PAGE_DIR/dist/index.html" ] && PAGE_WAS_BUILT=1

if [ ! -x "$VITE" ]; then
    echo "refusing: no vite at $VITE — run 'npm install' in $RELEASE/awm." >&2
    echo "A deploy that skips the page leaves the browser on the previous bundle" >&2
    echo "and says nothing about it." >&2
    exit 1
fi
# One page, not the sixteen `scripts/build.sh` walks. Same invocation shape:
# cwd is the page dir so outDir 'dist' and \$lib resolve page-locally.
if [ "$DRY" = 1 ]; then
    echo "  would run: (cd $PAGE_DIR && $VITE build --config $RELEASE/awm/vite.config.ts)"
else
    ( cd "$PAGE_DIR" && "$VITE" build --config "$RELEASE/awm/vite.config.ts" ) \
        | tail -4
fi

# ---------------------------------------------------------------------------
# 5. Make it live
# ---------------------------------------------------------------------------

# Two things a one-service restart cannot do: register a service the gateway
# has never discovered, and mount a page whose dist did not exist at boot.
# Either one means the whole gateway has to come back.
KNOWN=0
"$AWM_BIN" services list 2>/dev/null | awk '{print $1}' | grep -qx trilium && KNOWN=1
if [ "$KNOWN" = 0 ] || [ "$PAGE_WAS_BUILT" = 0 ]; then
    echo "  first deploy of the service or the page — using the full deploy path"
    FULL=1
fi

if [ "$FULL" = 1 ]; then
    say "awm deploy (install if the dist set moved, rebuild changed pages, restart)"
    run "$AWM_BIN" deploy
else
    say "restarting the trilium service"
    # One service, not the gateway. Every Trilium child dies with the adapter
    # and comes back on its next pass; a person loses a browser reload and no
    # notes, because Trilium persists to its data directory on every change.
    run "$AWM_BIN" services restart trilium
fi

# ---------------------------------------------------------------------------
# 6. Verify
# ---------------------------------------------------------------------------

say "verifying"
if [ "$DRY" = 1 ]; then echo "  would check services list, trilium status"; exit 0; fi

"$AWM_BIN" services list | awk 'NR==1 || $1=="trilium"'
echo
"$AWM_BIN" trilium status 2>&1 | head -40 || {
    echo "trilium status failed — the service registered but its verbs did not answer." >&2
    exit 1
}
