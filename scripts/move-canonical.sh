#!/usr/bin/env bash
# The awm half of moving the canonical repos from altair to mizar (routines.md
# R31, install.md R114). The cloud project's canonical_repos role moves the refs:
# it takes every branch and tag into mizar's bare repos, checks them, repoints
# each bare repo's origin and fences altair. This script does what that role
# does not: the workspace checkout's remotes and the GitHub backup remote.
#
#   scripts/move-canonical.sh order               print the sitting's order
#   scripts/move-canonical.sh github <url>        on mizar: add the backup remote
#   scripts/move-canonical.sh repoint <old> <new> on a node: move the checkout's remotes
#
# repoint moves every remote of the workspace checkout whose URL is <old> to
# <new>, then fetches from it. <new> is `local` on the node that holds the repo
# (mizar, and altair, which takes mizar's pushes into its own bare). Nothing here
# moves a ref, so a re-run is safe.
set -euo pipefail

WS=${AWM_WORKSPACE:-$HOME/agentic_workspace}
BARE=$WS/projects/awm/.bare

order() {
    cat <<'EOT'
1. cloud: canonical_repos main on mizar (take every ref from altair, then check).
2. cloud: canonical_repos repoint on every other node.
3. awm, on mizar:  scripts/move-canonical.sh github <the GitHub URL of awm>
4. awm, on mizar:  scripts/move-canonical.sh repoint altair:<WS>/projects/awm/.bare local
5. awm, on each node that reaches mizar:
                   scripts/move-canonical.sh repoint altair:<WS>/projects/awm/.bare mizar:<WS>/projects/awm/.bare
   altair keeps its local bare: its awm remote already points there.
6. cloud: canonical_repos stop on altair, only after step 1's check passed.
7. Each node's install config names mizar as [fleet] home, and altair under
   [fleet.push.altair] for awm, cloud and trilium.
EOT
}

github() {
    local url=${1:?usage: move-canonical.sh github <url>}
    if git -C "$BARE" remote get-url github >/dev/null 2>&1; then
        git -C "$BARE" remote set-url github "$url"
    else
        git -C "$BARE" remote add github "$url"
    fi
    local tip
    tip=$(git -C "$BARE" ls-remote --exit-code github refs/heads/release | cut -f1) || {
        echo "github has no release yet: the first deploy creates it"
        return 0
    }
    if git -C "$BARE" merge-base --is-ancestor "$tip" release 2>/dev/null; then
        echo "github release ${tip:0:12} is behind or at release: deploy takes it from here"
    else
        echo "github release ${tip:0:12} is not in release. Every deploy stops at backup until it is." >&2
        return 1
    fi
}

repoint() {
    local old=${1:?usage: move-canonical.sh repoint <old> <new>} new=${2:?}
    [ "$new" = local ] && new=$BARE
    local moved=0 name
    for name in $(git -C "$WS" remote); do
        [ "$(git -C "$WS" remote get-url "$name")" = "$old" ] || continue
        git -C "$WS" remote set-url "$name" "$new"
        git -C "$WS" fetch -q --no-tags "$name"
        echo "$name -> $new"
        moved=1
    done
    [ "$moved" = 1 ] || echo "no remote of $WS points at $old"
}

case "${1:-}" in
    order) order ;;
    github) shift; github "$@" ;;
    repoint) shift; repoint "$@" ;;
    *) echo "usage: move-canonical.sh order | github <url> | repoint <old> <new>" >&2; exit 2 ;;
esac
