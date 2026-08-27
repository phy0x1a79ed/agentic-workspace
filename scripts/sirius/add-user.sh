#!/usr/bin/env bash
# Create (or complete) a user of the public app: an auth account, a scope
# worktree `projects/userdata/<name>` on branch `user/<name>` with the
# notes/, drawio/ and data/figures/ folders, DVC initialised with a local
# cache. Idempotent — every step checks before it acts.
#
#   scripts/sirius/add-user.sh <name>
#
# Host-agnostic: the gateway is reached on loopback through the awm CLI, and
# the git/dvc steps run as whoever owns the projects directory (the `awm`
# system user on sirius via sudo, the dev user on a dev box). Needs the
# gateway up with the auth and scopes services. Prints the password once:
# there is no other way to read it back (`awm auth user_passwd` resets it).
set -euo pipefail
NAME=${1:?usage: $0 <name>}
[[ "$NAME" =~ ^[a-z][a-z0-9_-]{0,31}$ ]] || { echo "bad name: $NAME (^[a-z][a-z0-9_-]{0,31}$)" >&2; exit 1; }

WS=${AWM_WORKSPACE:-$(git -C "$(dirname "$0")" rev-parse --show-toplevel)}
PROJECTS=$(readlink -f "$WS/projects")
OWNER=$(stat -c %U "$PROJECTS")
if [ "$OWNER" = "$(id -un)" ]; then run() { "$@"; }; else run() { sudo -u "$OWNER" env HOME="$(getent passwd "$OWNER" | cut -d: -f6)" "$@"; }; fi
DVC=${AWM_DVC_BIN:-$(command -v dvc || ls "$HOME"/lib/miniforge3/envs/{dvc,awm}/bin/dvc /opt/miniforge3/envs/awm/bin/dvc 2>/dev/null | head -1)}
[ -x "${DVC:-}" ] || { echo "dvc not found (set AWM_DVC_BIN)" >&2; exit 1; }
GIT=(git -c user.name=awm -c user.email=awm@localhost)

step() { echo "== $*"; }

step "project userdata"
awm project search --query userdata 2>/dev/null | grep -q '"name": "userdata"' \
    || awm project create --name userdata >/dev/null

step "scope userdata/$NAME"
awm scope search --project userdata --query "$NAME" 2>/dev/null | grep -q "\"scope\": \"$NAME\"" \
    || awm scope create --project userdata --scope "$NAME" --branch-name "user/$NAME" >/dev/null
ROOT="$PROJECTS/userdata/$NAME"
[ -d "$ROOT" ] || { echo "scope worktree missing at $ROOT" >&2; exit 1; }

step "folders"
for d in notes drawio data/figures; do
    run mkdir -p "$ROOT/$d"
    [ -e "$ROOT/$d/.gitkeep" ] || run touch "$ROOT/$d/.gitkeep"
done

step "dvc"
if [ ! -f "$ROOT/.dvc/config" ]; then
    run "$DVC" init -q --subdir 2>/dev/null || run "$DVC" init -q
fi
run "$DVC" config --local -q core.autostage true 2>/dev/null || true
run "$DVC" config --local -q cache.dir "$(readlink -f "$WS/data")/.dvc_cache"
run "$DVC" config --local -q cache.type hardlink,symlink

step "commit"
run "${GIT[@]}" -C "$ROOT" add -A -- notes drawio data .dvc .dvcignore 2>/dev/null || true
if ! run git -C "$ROOT" diff --cached --quiet; then
    run "${GIT[@]}" -C "$ROOT" commit -q -m "userdata: scaffold $NAME" -m "Author-Handle: system"
fi
awm scope heal --project userdata >/dev/null 2>&1 || true

step "auth account"
if awm auth user_list | grep -q "\"username\": \"$NAME\""; then
    echo "   account exists (reset with: awm auth user_passwd --username $NAME)"
else
    awm auth user_add --username "$NAME"
fi
echo "ready: $ROOT (branch user/$NAME)"
