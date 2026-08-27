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
#   5. the per-user Trilium vhosts, when TRILIUM_DOMAIN names a parent domain
#   6. /usr/local/bin/{awm,awm-mcp}, awm.service, restart
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
PUBLIC_SERVICES="auth httpsfront notes drawio fileviewer scopes trilium"
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
        v=false; [ -n "$PUBLIC_SERVICES" ] && grep -qw -- "$n" <<<"$PUBLIC_SERVICES" && v=true
        printf '%s  "%s": %s' "$sep" "$n" "$v"; sep=$',\n'
    done
    echo; echo "}"
} > "$want"
if ! sudo cmp -s "$want" "$ENABLED"; then
    sudo -u "$APP_USER" tee "$ENABLED" < "$want" >/dev/null
    echo "   wrote $ENABLED (public service set: ${PUBLIC_SERVICES:-none})"
fi
rm -f "$want"

step "trilium vhosts"
# The parent name the per-user vhosts hang off, e.g. `tony.notes.example.com`.
# Unset: no vhost, and each person's Trilium answers on loopback only.
#
# One subdomain per person, never one path prefix. Trilium has no URL-base
# setting, so an SPA served under `/trilium/<user>/` asks for its own assets at
# `/` and paints a shell that never finishes loading.
#
# WARNING: every <user>.$TRILIUM_DOMAIN needs its own DNS record, or one
# wildcard, before it resolves. Cloudflare must proxy each one: ufw admits 80
# and 443 from Cloudflare's ranges alone.
TRILIUM_DOMAIN="${TRILIUM_DOMAIN:-}"
if [ -z "$TRILIUM_DOMAIN" ]; then
    echo "   TRILIUM_DOMAIN unset — no vhost; each Trilium answers on loopback only"
else
    tmpconf=$(mktemp)
    AWM_BIN="$MF/envs/awm/bin/awm" bash "$HERE/trilium-nginx.sh" "$TRILIUM_DOMAIN" "$tmpconf"
    sudo install -m 644 "$tmpconf" /etc/nginx/sites-available/trilium.conf
    sudo ln -sfn /etc/nginx/sites-available/trilium.conf /etc/nginx/sites-enabled/trilium.conf
    rm -f "$tmpconf"
    # `nginx -t` before the reload. A bad config reloaded leaves the box with no
    # web server at all, and nginx is the only way the public reaches anything.
    sudo nginx -t -q && sudo systemctl reload nginx
    echo "   $(grep -c '^server {' /etc/nginx/sites-available/trilium.conf) vhost(s) under $TRILIUM_DOMAIN"
fi

step "built pages"
for p in notes drawio trilium; do
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
