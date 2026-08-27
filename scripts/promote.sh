#!/usr/bin/env bash
# Promote a feature branch: feat -> dev -> release -> altair -> sirius.
#
#   scripts/promote.sh <feat-branch> [--to dev|release|sirius]
#
# Run on altair from any awm scope worktree. Every stage is gated on the one
# before and idempotent, so a re-run with nothing new reports no changes:
#   1. merge <feat> into projects/awm/dev (the dev hub; `scope gather`
#      refuses it because its branch is the flat `dev`)
#   2. the full test suite in the dev worktree
#   3. release in the local bare fast-forwards to origin/release (another
#      node may have promoted), then dev is merged into it (release is a
#      superset of dev: fixes sometimes land on release directly); the
#      workspace-root checkout fast-forwards from the local bare; dev,
#      release and <feat> go to origin
#   4. `awm deploy` in the root checkout (install/build/restart as needed;
#      a changed drawio client patch also re-runs that service's install.sh,
#      which `awm deploy` does not notice)
#   5. pages built in the root checkout, `scripts/sirius/deploy.sh release`
# It ends by printing the sha at every hop and refuses to finish unless they
# agree. capella and mira are not part of this path.
#
# The workspace-root checkout is a deploy target: nothing here commits in it.
set -euo pipefail

FEAT=${1:?usage: $0 <feat-branch> [--to dev|release|sirius]}
TO=sirius
[ "${2:-}" = "--to" ] && TO=${3:?--to dev|release|sirius}
case "$TO" in dev|release|sirius) ;; *) echo "bad --to $TO" >&2; exit 1;; esac

WSROOT=/home/tony/agentic_workspace
BARE=$WSROOT/projects/awm/.bare
DEV=$WSROOT/projects/awm/dev
PATH="$HOME/lib/miniforge3/envs/awm/bin:$PATH"

step() { echo; echo "== $*"; }
sha() { git -C "$1" rev-parse --short "${2:-HEAD}"; }

[ -d "$DEV/.git" ] || [ -f "$DEV/.git" ] || { echo "no dev worktree at $DEV" >&2; exit 1; }
git -C "$BARE" rev-parse --verify -q "refs/heads/$FEAT" >/dev/null || { echo "no branch $FEAT in $BARE" >&2; exit 1; }
[ "$(git -C "$DEV" rev-parse --abbrev-ref HEAD)" = dev ] || { echo "$DEV is not on dev" >&2; exit 1; }
[ -z "$(git -C "$DEV" status --porcelain)" ] || { echo "$DEV has uncommitted changes; commit or clear them first" >&2; git -C "$DEV" status --short | head; exit 1; }

step "1. dev <- $FEAT"
if git -C "$DEV" merge-base --is-ancestor "$FEAT" dev; then
    echo "   dev already contains $FEAT ($(sha "$DEV" "$FEAT"))"
else
    git -C "$DEV" merge --no-edit "$FEAT"
fi
echo "   dev @ $(sha "$DEV")"

step "2. tests (dev)"
# PROMOTE_TEST_DISTS="auth httpsfront" narrows the run to named dists.
# shellcheck disable=SC2086
if ! "$DEV/awm/gateway/scripts/run-tests.sh" ${PROMOTE_TEST_DISTS:-} >"$DEV/.awm/promote-tests.log" 2>&1; then
    grep -E '^(FAILED|ERROR)|FAIL \(' "$DEV/.awm/promote-tests.log" | head -40
    echo "tests failed; log: $DEV/.awm/promote-tests.log" >&2
    exit 1
fi
grep -E '^  \S+ +(PASS|FAIL)' "$DEV/.awm/promote-tests.log" | tr -s ' ' | paste -sd' '
[ "$TO" = dev ] && { echo; echo "stopped after dev @ $(sha "$DEV")"; exit 0; }

step "3. release <- dev"
git -C "$BARE" fetch -q origin
if ! git -C "$BARE" merge-base --is-ancestor origin/release release; then
    git -C "$BARE" merge-base --is-ancestor release origin/release \
        || { echo "local release and origin/release diverged; reconcile by hand" >&2; exit 1; }
    git -C "$BARE" update-ref refs/heads/release origin/release
    echo "   release fast-forwarded to origin/release ($(sha "$BARE" release))"
fi
if git -C "$BARE" merge-base --is-ancestor dev release; then
    echo "   release already contains dev"
else
    TMP=$(mktemp -d "$WSROOT/projects/awm/.promote-XXXX")
    trap 'git -C "$BARE" worktree remove --force "$TMP" 2>/dev/null || rm -rf "$TMP"' EXIT
    git -C "$BARE" worktree add -q "$TMP" release
    git -C "$TMP" -c user.name="$(git config user.name || echo awm)" -c user.email="$(git config user.email || echo awm@localhost)" \
        merge --no-edit -m "Merge dev into release: $FEAT" dev
    git -C "$BARE" worktree remove --force "$TMP"
    trap - EXIT
fi
git -C "$WSROOT" fetch -q "$BARE" release
git -C "$WSROOT" merge -q --ff-only FETCH_HEAD
git -C "$BARE" push -q origin dev release "$FEAT"
echo "   bare release @ $(sha "$BARE" release)  root @ $(sha "$WSROOT")  origin/release @ $(git -C "$BARE" rev-parse --short origin/release)"

step "4. altair deploy"
before=$(git -C "$WSROOT" rev-parse HEAD@{1} 2>/dev/null || true)
(cd "$WSROOT" && awm deploy)
if [ -n "$before" ] && [ -n "$(git -C "$WSROOT" diff --name-only "$before" HEAD -- awm/services/drawio/patches 2>/dev/null)" ]; then
    echo "   drawio client patches changed: re-running its install.sh"
    (cd "$WSROOT" && bash awm/services/drawio/install.sh)
fi
# trilium falls into the same trap as drawio — `awm deploy` re-runs a service's
# install only when the *set* of installed dists changes, so a rebuilt or
# re-downloaded server bundle never lands — but it cannot be caught the same
# way. The Trilium fork is a *separate repository* (projects/trilium), so no
# diff over this checkout can see it move, and `before` is read from the reflog
# and is empty whenever stage 3 was a no-op. So this is unconditional. The
# install is stamped on the fork HEAD, its dirty flag and its lockfile (or the
# asset name, on a tarball node) and costs seconds when nothing moved.
if [ -z "${PROMOTE_SKIP_TRILIUM:-}" ] && [ -f "$WSROOT/awm/services/trilium/install.sh" ]; then
    echo "   trilium: re-running install.sh (stamped; a no-op when nothing moved)"
    (cd "$WSROOT" && bash awm/services/trilium/install.sh)
fi
[ "$TO" = release ] && { echo; echo "stopped after altair @ $(sha "$WSROOT")"; exit 0; }

step "5. sirius"
(cd "$WSROOT/awm" && npm run build >/dev/null)
(cd "$WSROOT" && bash scripts/sirius/deploy.sh release)

step "verify"
r_bare=$(git -C "$BARE" rev-parse release)
r_root=$(git -C "$WSROOT" rev-parse HEAD)
r_origin=$(git -C "$BARE" rev-parse origin/release)
r_sirius=$(ssh sirius 'git -C /opt/awm rev-parse HEAD')
printf '   %-8s %s\n' bare "${r_bare:0:9}" root "${r_root:0:9}" origin "${r_origin:0:9}" sirius "${r_sirius:0:9}"
[ "$r_bare" = "$r_root" ] && [ "$r_bare" = "$r_origin" ] && [ "$r_bare" = "$r_sirius" ] \
    || { echo "release is not the same sha everywhere" >&2; exit 1; }
echo "promoted $FEAT -> release @ ${r_bare:0:9} on altair and sirius"
