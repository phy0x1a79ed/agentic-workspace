#!/usr/bin/env bash
# Create (or complete) a user of the public app: an auth account and a scope
# worktree `projects/userdata/<name>` on branch `user/<name>` with the
# notes/, drawio/ and data/figures/ folders, DVC initialised with a local
# cache. Idempotent — every step checks before it acts.
#
# This is the whole of adding a person. There is no DNS record to create, no
# port to allocate and no web-server config to write: the shared vault is
# served by the edge at /trilium/ to anyone with an account, so an account is all
# it takes. Nothing here touches the vault.
#
#   scripts/sirius/add-user.sh <name>
#
# Host-agnostic: the gateway is reached on loopback through the awm CLI, and
# the git/dvc steps run as whoever owns the projects directory (the `awm`
# system user on sirius via sudo, the dev user on a dev box). Needs the
# gateway up with the auth and scopes services. Prints the password once:
# there is no other way to read it back (`awm auth user-passwd` resets it).
set -euo pipefail
NAME=${1:?usage: $0 <name>}
[[ "$NAME" =~ ^[a-z][a-z0-9_-]{0,31}$ ]] || { echo "bad name: $NAME (^[a-z][a-z0-9_-]{0,31}$)" >&2; exit 1; }

WS=${AWM_WORKSPACE:-$(git -C "$(dirname "$0")" rev-parse --show-toplevel)}
# On sirius projects/ is a symlink into /var/lib/awm, which the dev user
# cannot traverse: every probe below goes through `run`.
PROJECTS=$(readlink "$WS/projects" 2>/dev/null || echo "$WS/projects")
DATA=$(readlink "$WS/data" 2>/dev/null || echo "$WS/data")
OWNER=$(stat -c %U "$PROJECTS" 2>/dev/null || sudo stat -c %U "$PROJECTS")
if [ "$OWNER" = "$(id -un)" ]; then run() { "$@"; }; else run() { sudo -u "$OWNER" env HOME="$(getent passwd "$OWNER" | cut -d: -f6)" "$@"; }; fi
DVC=${AWM_DVC_BIN:-}
for cand in "$(command -v dvc || true)" "$HOME/lib/miniforge3/envs/dvc/bin/dvc" /opt/miniforge3/envs/dvc/bin/dvc; do
    [ -n "$DVC" ] && break
    if [ -n "$cand" ] && [ -x "$cand" ]; then DVC=$cand; fi
done
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
run test -d "$ROOT" || { echo "scope worktree missing at $ROOT" >&2; exit 1; }

step "folders"
for d in notes drawio data/figures; do
    run mkdir -p "$ROOT/$d"
    run test -e "$ROOT/$d/.gitkeep" || run touch "$ROOT/$d/.gitkeep"
done

step "dvc"
if ! run test -f "$ROOT/.dvc/config"; then
    run "$DVC" --cd "$ROOT" init -q --subdir 2>/dev/null || run "$DVC" --cd "$ROOT" init -q
fi
run "$DVC" --cd "$ROOT" config --local -q core.autostage true 2>/dev/null || true
run "$DVC" --cd "$ROOT" config --local -q cache.dir "$DATA/.dvc_cache"
run "$DVC" --cd "$ROOT" config --local -q cache.type hardlink,symlink

step "commit"
# Only the scaffold's own files: the services commit what users write.
run "${GIT[@]}" -C "$ROOT" add -- notes/.gitkeep drawio/.gitkeep data/figures/.gitkeep .dvc .dvcignore 2>/dev/null || true
if ! run git -C "$ROOT" diff --cached --quiet; then
    run "${GIT[@]}" -C "$ROOT" commit -q -m "userdata: scaffold $NAME" -m "Author-Handle: system"
fi
awm scope heal --project userdata >/dev/null 2>&1 || true

step "auth account"
if awm auth user-list | grep -q "\"username\": \"$NAME\""; then
    echo "   account exists (reset with: awm auth user-passwd --username $NAME)"
else
    awm auth user-add --username "$NAME"
fi
echo "ready: $ROOT (branch user/$NAME)"
echo "       the shared vault is at /trilium/ — nothing further to set up"
