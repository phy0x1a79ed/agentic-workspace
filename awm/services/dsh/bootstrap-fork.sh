#!/usr/bin/env bash
# Create the harness fork this node serves. Run once per node, deliberately.
#
# `install.sh` never does this: cloning 109 MB is not something a deploy may do
# behind your back. It warns and leaves the service registered-but-unbuilt until
# this script runs.
#
# The layout it produces:
#
#   master   upstream mirror, no worktree, never worked in
#   dev      every change we author, cut from $BASE_REF
#   release  what the deployed service serves, cut from $BASE_REF
#
# Override the base ref with DSH_BASE_REF=<tag>.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ENV="${AWM_ENV:-awm}"

# The workspace holding `projects/`, not the git toplevel: run from a scope
# worktree the two differ, and only this one has a `projects/` to create in.
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
    echo "error: no workspace root above $1 — set AWM_WORKSPACE." >&2
    return 1
}
WS="$(workspace_root "$HERE")"

PROJECT="deepseek-harness"
UPSTREAM_URL="https://github.com/deepseek-ai/${PROJECT}"
BASE_REF="${DSH_BASE_REF:-dsh-v0.1.1-rc.2}"
BARE="$WS/projects/$PROJECT/.bare"

awm_cli() {
    if command -v awm >/dev/null 2>&1; then
        awm "$@"
    else
        mamba run -n "$ENV" --no-capture-output awm "$@"
    fi
}

if [ -d "$BARE" ]; then
    echo "Project '$PROJECT' exists at $BARE."
else
    echo "Forking $UPSTREAM_URL …"
    awm_cli project create --name "$PROJECT" --fork-url "$UPSTREAM_URL"
fi

git -C "$BARE" fetch --tags --quiet origin
git -C "$BARE" fetch --tags --quiet upstream || true

if ! git -C "$BARE" rev-parse --verify --quiet "$BASE_REF" >/dev/null; then
    echo "error: base ref '$BASE_REF' not found in $BARE" >&2
    echo "  available: $(git -C "$BARE" tag --sort=-creatordate | head -4 | tr '\n' ' ')" >&2
    exit 1
fi
BASE_SHA="$(git -C "$BARE" rev-parse "$BASE_REF")"

for scope in dev release; do
    if [ -d "$WS/projects/$PROJECT/$scope" ]; then
        echo "Scope '$scope' exists."
    else
        echo "Creating scope '$scope' at $BASE_REF …"
        awm_cli scope create --project "$PROJECT" --scope "$scope" \
            --branch-name "$scope" --from-branch "$BASE_REF"
    fi
done

# `project create` scaffolds a worktree on the default branch. Retire it: a
# branch we never author in has no use for one, and it would drift.
#
# CAUTION: `scope delete` deletes the branch as well as the worktree, and git
# will not stop it from taking the branch the bare repo's HEAD points at. The
# symref is left dangling, `_detect_default_branch` then reads a ref that does
# not resolve, and the next `worktree add` dies with `Not a valid object name:
# 'HEAD'`. Restoring both afterwards is what the rest of this block is for.
DEFAULT_BRANCH="$(git -C "$BARE" symbolic-ref --short HEAD 2>/dev/null || echo master)"
if [ -d "$WS/projects/$PROJECT/$DEFAULT_BRANCH" ]; then
    DEFAULT_SHA="$(git -C "$BARE" rev-parse "$DEFAULT_BRANCH")"
    echo "Retiring the scaffolded '$DEFAULT_BRANCH' worktree …"
    awm_cli scope delete --project "$PROJECT" --scope "$DEFAULT_BRANCH" --force
    git -C "$BARE" rev-parse --verify --quiet "$DEFAULT_BRANCH" >/dev/null \
        || git -C "$BARE" branch "$DEFAULT_BRANCH" "$DEFAULT_SHA"
    git -C "$BARE" symbolic-ref HEAD "refs/heads/$DEFAULT_BRANCH"
fi

# Fail loudly rather than leave a half-shaped project: every later step assumes
# this exact layout.
test -n "$(git -C "$BARE" rev-parse --verify --quiet "$DEFAULT_BRANCH")"
test "$(git -C "$BARE" symbolic-ref HEAD)" = "refs/heads/$DEFAULT_BRANCH"
test ! -d "$WS/projects/$PROJECT/$DEFAULT_BRANCH"
for scope in dev release; do
    test -e "$WS/projects/$PROJECT/$scope/.git"
    test "$(git -C "$WS/projects/$PROJECT/$scope" rev-parse "$scope")" = "$BASE_SHA" \
        || echo "note: $scope has moved past $BASE_REF, which is expected once it carries commits."
done

echo
echo "Project:  $WS/projects/$PROJECT"
git -C "$BARE" branch -v
echo
echo "Next: ./install.sh builds 'release' and the dsh service serves it."
