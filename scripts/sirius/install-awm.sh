#!/usr/bin/env bash
# Install or update awm on sirius. Run as the dev user, from the checkout:
#
#   /opt/awm/scripts/sirius/install-awm.sh
#
# The only sanctioned way to put awm on this box. It refuses to run from any
# checkout but /opt/awm, so a future session cannot seed a second install in
# a home directory. Idempotent. Steps:
#   1. miniforge at /opt/miniforge3 (dev-user owned)
#   2. mamba env `awm` + awm/gateway/install.sh (editable installs of the
#      public service set, no search extra); mamba env `dvc`
#   3. .awm and the other gitignored write dirs -> symlinks into /var/lib/awm
#   4. `awm gateway init` as the app user, enabled.json reconciled to the set
#   5. remove the retired per-person Trilium vhosts and env keys, if any
#   6. the vault's directory under the Trilium project, so the knowledge base
#      has somewhere to live
#   7. /usr/local/bin/{awm,awm-mcp}, awm.service, restart
set -euo pipefail

INSTALL_ROOT=/opt/awm
STATE_ROOT=/var/lib/awm
MF=/opt/miniforge3
APP_USER=awm
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(git -C "$HERE" rev-parse --show-toplevel)"

[ "$ROOT" = "$INSTALL_ROOT" ] || { echo "refusing: this checkout is $ROOT, awm on this box lives at $INSTALL_ROOT" >&2; exit 1; }
[ "$(id -u)" -ne 0 ] || { echo "run as the dev user, not root" >&2; exit 1; }
export AWM_WORKSPACE=$INSTALL_ROOT
cd "$INSTALL_ROOT"

step() { echo; echo "== $*"; }

step "miniforge"
if [ ! -x "$MF/bin/mamba" ]; then
    inst=$(mktemp --suffix=.sh)
    curl -fsSL -o "$inst" "https://github.com/conda-forge/miniforge/releases/latest/download/Miniforge3-$(uname)-$(uname -m).sh"
    bash "$inst" -b -u -p "$MF"
    rm -f "$inst"
fi
export PATH="$MF/bin:$PATH"
export MAMBA_ROOT_PREFIX=$MF

step "env + editable installs"
if [ ! -x "$MF/envs/awm/bin/python" ]; then
    mamba env create -y -q -f awm/gateway/environment.yml
else
    mamba env update -y -q -f awm/gateway/environment.yml --prune
fi
# The service set installed and enabled on this box. Every other service
# under awm/services/ is written as disabled. scopes is here for the CLI
# (add-user.sh) only: the public edge (httpsfront, AWM_EDGE_PROFILE=public)
# never forwards /svc/scopes.
PUBLIC_SERVICES="auth httpsfront fileviewer scopes trilium penpot-view penpot-plugins zotero tether"
# No torch/sentence-transformers on a 4 GB box: FTS search only.
AWM_ENV=awm AWM_SERVICES="$PUBLIC_SERVICES" AWM_SEARCH=0 bash awm/gateway/install.sh
# dvc in its own env, as on altair: its dependency set is not the gateway's.
[ -x "$MF/envs/dvc/bin/dvc" ] || mamba create -y -q -n dvc -c conda-forge dvc

step "state symlinks"
for d in .awm data projects tasks main; do
    target=$STATE_ROOT/${d#.awm}; [ "$d" = .awm ] && target=$STATE_ROOT/state
    if [ -e "$d" ] && [ ! -L "$d" ]; then
        echo "refusing: $INSTALL_ROOT/$d is a real directory, expected a symlink to $target" >&2; exit 1
    fi
    [ "$(readlink "$d" 2>/dev/null)" = "$target" ] || ln -sfn "$target" "$d"
done

step "init as $APP_USER"
# The scopes service commits as the app user (project seeds, scaffolds).
sudo -u "$APP_USER" env HOME=$STATE_ROOT git config --global user.name awm
sudo -u "$APP_USER" env HOME=$STATE_ROOT git config --global user.email awm@localhost
sudo -u "$APP_USER" env AWM_WORKSPACE=$INSTALL_ROOT HOME=$STATE_ROOT XDG_CONFIG_HOME=$STATE_ROOT/config \
    "$MF/envs/awm/bin/python" -m awm.gateway gateway init
ENABLED=$STATE_ROOT/state/services/enabled.json
sudo -u "$APP_USER" install -d -m 750 "$(dirname "$ENABLED")"
want=$(mktemp)
{
    echo "{"
    sep=""
    for d in awm/services/*/; do
        n=$(basename "$d")
        # Exact token match, not `grep -qw`: -w treats a hyphen as a word boundary,
    # so the word `penpot` matches inside `penpot-view`. With the stack
    # supervisor deliberately kept off this box and only the render service
    # installed, a word match would enable the very service being excluded.
    # An empty list renders as two spaces and matches nothing, so the old
    # -n guard is no longer needed.
    v=false; case " $PUBLIC_SERVICES " in *" $n "*) v=true ;; esac
        printf '%s  "%s": %s' "$sep" "$n" "$v"; sep=$',\n'
    done
    echo; echo "}"
} > "$want"
# A run that would turn a service *off* is almost never what anyone meant. The
# recorded incident is a feature branch carrying a stale copy of the
# PUBLIC_SERVICES line above, which would have cut the live public set to one
# and reported success doing it. Procedure alone did not catch it, so the
# script compares against what the box already serves and refuses.
if sudo test -f "$ENABLED"; then
    enabled_names() { grep -oE '"[A-Za-z0-9_-]+": true' "$1" | cut -d'"' -f2 | sort; }
    losing=$(comm -23 <(sudo -u "$APP_USER" grep -oE '"[A-Za-z0-9_-]+": true' "$ENABLED" | cut -d'"' -f2 | sort) \
                      <(enabled_names "$want") | tr '\n' ' ')
    if [ -n "${losing// /}" ] && [ -z "${AWM_ALLOW_DISABLE:-}" ]; then
        echo "refusing: this run would disable services the box is serving: ${losing% }" >&2
        echo "  PUBLIC_SERVICES in this checkout is probably stale — reconcile it with release." >&2
        echo "  Set AWM_ALLOW_DISABLE=1 if turning them off is genuinely the intent." >&2
        rm -f "$want"; exit 1
    fi
fi
if ! sudo cmp -s "$want" "$ENABLED"; then
    sudo -u "$APP_USER" tee "$ENABLED" < "$want" >/dev/null
    echo "   wrote $ENABLED (public service set: ${PUBLIC_SERVICES:-none})"
fi
rm -f "$want"

step "retire the per-person trilium wiring"
# Removal, not omission. /etc/awm/env keys are add-once and no deploy ever
# deletes an nginx vhost, so a box provisioned under the old shape keeps both
# forever unless something takes them away. Leaving either is the one failure
# this design must not have: the vault now runs with its own authentication
# off, and a vhost pointing straight at its loopback port would be an
# unauthenticated public knowledge base.
AWM_ENV_FILE=/etc/awm/env
for f in /etc/nginx/sites-enabled/trilium.conf /etc/nginx/sites-available/trilium.conf; do
    if sudo test -e "$f"; then
        sudo rm -f "$f"
        echo "   removed $f"
        nginx_dirty=1
    fi
done
if sudo test -f "$AWM_ENV_FILE" && sudo grep -qE '^(TRILIUM_FRONTS|TRILIUM_DOMAIN)=' "$AWM_ENV_FILE"; then
    sudo sed -i -E '/^(TRILIUM_FRONTS|TRILIUM_DOMAIN)=/d' "$AWM_ENV_FILE"
    echo "   $AWM_ENV_FILE: dropped TRILIUM_FRONTS / TRILIUM_DOMAIN"
fi
if [ -n "${nginx_dirty:-}" ]; then
    # `nginx -t` before the reload. A bad config reloaded leaves the box with no
    # web server at all, and nginx is the only way the public reaches anything.
    sudo nginx -t -q && sudo systemctl reload nginx
fi

step "vault directory"
# The vault's data sits where the service looks for it, which is the Trilium
# project's release scope on every node. Here that is a plain directory and
# nothing else: this box serves the published tarball, holds no fork, and so
# needs no git or DVC for source that is not there.
#
# Best-effort, like the rest of this script. The gateway runs every service's
# install under `set -e`, so a hard failure here would abort the whole deploy on
# a box that is otherwise fine — the trap an unguarded mkdir sprang once
# already.
#
# live/ is runtime state: the database, its write-ahead log, the session store
# and Trilium's own rolling backups. data/vault/ takes the named snapshots and
# the mirrored Zotero library. Neither is pinned here. Pinning needs a checkout,
# and without one the service writes its files and says it committed nothing.
VROOT="$ROOT/projects/trilium/release"
if sudo -u "$APP_USER" mkdir -p "$VROOT/live" "$VROOT/data/vault"; then
    echo "   vault directory at $VROOT"
else
    echo "   WARNING: no vault directory at $VROOT — the service will report this via status"
fi

step "built pages"
for p in drawio trilium; do
    [ -f "awm/pages/$p/dist/index.html" ] || echo "   WARNING: awm/pages/$p/dist missing — deploy.sh ships it from the dev box"
done

step "entry points + unit"
sudo ln -sfn "$MF/envs/awm/bin/awm" /usr/local/bin/awm
sudo ln -sfn "$MF/envs/awm/bin/awm-mcp" /usr/local/bin/awm-mcp
sudo install -m 644 "$HERE/etc/systemd/awm.service" /etc/systemd/system/awm.service
sudo systemctl daemon-reload
sudo systemctl enable -q awm
sudo systemctl restart awm
sleep 2
systemctl is-active awm >/dev/null || { sudo journalctl -u awm -n 40 --no-pager; exit 1; }
echo
echo "awm.service active: $(systemctl show -p MainPID --value awm)"
