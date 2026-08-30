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
step "penpot account"
# Penpot keeps accounts of its own, but nobody is ever told a Penpot password.
# awm holds one per person: this block creates the profile, hands the credential
# to the auth service, and says nothing further. The edge exchanges it for a
# Penpot session on each page load and the auth service replaces it nightly, so
# one awm sign-in is the whole of getting into a diagram -- which is what keeps
# this script the whole of adding a person, the CAUTION at the top of this file.
#
# Driven over the backend's PREPL, which binds container-localhost only, so
# this opens no network port. Skipped entirely where there is no stack, which
# is how a dev box runs this script unchanged.
PENPOT_COMPOSE_DIR=${PENPOT_COMPOSE_DIR:-/etc/awm/penpot}
# /etc/awm is root:awm 0750 and the dev user who runs this script is not in
# group awm, so a plain `[ -f ]` answers "no stack" on the one box that has
# one -- and then says so, which reads as a fact about the box rather than
# about the check. Ask again through sudo, which the block needs anyway.
# `sudo -n` so a box without passwordless sudo answers no instead of prompting.
penpot_stack_here() {
    [ -f "$PENPOT_COMPOSE_DIR/docker-compose.yml" ] && return 0
    sudo -n test -f "$PENPOT_COMPOSE_DIR/docker-compose.yml" 2>/dev/null
}
if penpot_stack_here && command -v docker >/dev/null; then
    PENPOT_EMAIL="$NAME@nexus.tony-xy-liu.com"
    pcompose() {
        sudo docker compose -p awm-penpot \
            -f "$PENPOT_COMPOSE_DIR/docker-compose.yml" \
            -f "$PENPOT_COMPOSE_DIR/docker-compose.sirius.yml" "$@"
    }
    # Penpot demands 8+ characters with a lowercase, an uppercase, a digit and
    # a special one, and refuses anything else -- so `openssl rand -hex`, which
    # draws neither of the last two, would produce a credential the nightly
    # rotation could never replace. Same alphabet and same rule as
    # `awm.auth.penpot.new_password`; the two have to agree or a rotation is
    # refused the first time it runs. `cut` rather than a second `head`: under
    # `pipefail` an early-exiting reader kills `tr` with SIGPIPE and fails the
    # whole pipeline.
    penpot_password() {
        local pw
        while :; do
            pw=$(head -c 512 /dev/urandom \
                 | LC_ALL=C tr -dc 'A-Za-z0-9!#%*+=?@^_' | cut -c1-28)
            case "$pw" in
                *[a-z]*) ;; *) continue ;;
            esac
            case "$pw" in
                *[A-Z]*) ;; *) continue ;;
            esac
            case "$pw" in
                *[0-9]*) ;; *) continue ;;
            esac
            case "$pw" in
                *[\!\#%\*+=?@^_]*) ;; *) continue ;;
            esac
            printf '%s' "$pw"
            return
        done
    }
    # The password reaches two processes as an argument, here and nowhere else.
    # It is never echoed, never written to a file and never returned by the verb
    # that records it -- `awm auth penpot-record` answers with the email and the
    # rotation time only.
    record_penpot() {
        awm auth penpot-record --username "$NAME" --email "$PENPOT_EMAIL" \
            --password "$1" >/dev/null
    }
    if pcompose exec -T penpot-backend python3 manage.py search-profile \
            -e "$PENPOT_EMAIL" 2>/dev/null | grep -q "$PENPOT_EMAIL"; then
        if awm auth penpot-list 2>/dev/null | grep -q "\"username\": \"$NAME\""; then
            echo "   penpot account exists; awm already holds the credential"
        else
            # The profile exists but awm holds no credential for it, so nobody
            # can sign in to it. A Penpot password cannot be read back, so the
            # repair is to set a new one and record that -- the same two-step
            # documented in awm/auth/penpot.py, run here automatically because
            # this is the state a box reaches when the profile predates this
            # design or the auth DB was rebuilt.
            PENPOT_PASS=$(penpot_password)
            if pcompose exec -T penpot-backend python3 manage.py update-profile \
                    -e "$PENPOT_EMAIL" -p "$PENPOT_PASS" && record_penpot "$PENPOT_PASS"; then
                echo "   penpot credential reset and handed to awm"
            else
                echo "   !! penpot credential not recorded; diagrams will ask for a password" >&2
            fi
            unset PENPOT_PASS
        fi
    else
        PENPOT_PASS=$(penpot_password)
        if pcompose exec -T penpot-backend python3 manage.py create-profile \
                -e "$PENPOT_EMAIL" -n "$NAME" -p "$PENPOT_PASS" \
                --skip-tutorial --skip-walkthrough && record_penpot "$PENPOT_PASS"; then
            echo "   penpot account created; awm holds the credential"
        else
            echo "   !! penpot account not created; diagrams will be unavailable" >&2
        fi
        unset PENPOT_PASS
    fi
else
    echo "   no penpot stack at $PENPOT_COMPOSE_DIR — skipped"
fi

echo "ready: $ROOT (branch user/$NAME)"
echo "       the shared vault is at /trilium/ — nothing further to set up"
echo "       diagrams are at /penpot/ — the same sign-in, no second password"
