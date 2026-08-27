#!/usr/bin/env bash
# Install or update awm on sirius. Run as the dev user, from the checkout:
#
#   /opt/awm/scripts/sirius/install-awm.sh
#
# The only sanctioned way to put awm on this box. It refuses to run from any
# checkout but /opt/awm, so a future session cannot seed a second install in
# a home directory. Idempotent. Steps:
#   1. miniforge at /opt/miniforge3 (dev-user owned)
#   2. mamba env `awm` + awm/gateway/install.sh (editable installs)
#   3. .awm and the other gitignored write dirs -> symlinks into /var/lib/awm
#   4. `awm gateway init` as the app user, enabled.json (the public set on)
#   5. per-user Trilium scopes + the nginx vhost that reaches them
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
# The service set installed and enabled on this box. Empty: the gateway alone.
# A name here is installed by the loop below and switched on in enabled.json.
PUBLIC_SERVICES="trilium"

# The people who get a Trilium. One server, one database and one login each —
# which is what tells two people apart, since the awm edge session is a single
# shared password. A name here becomes a scope directory below, and the service
# discovers it from the filesystem with no second list to keep in step.
TRILIUM_USERS="${TRILIUM_USERS:-tony}"

# The parent name the per-user vhosts hang off, e.g. `tony.notes.example.com`.
# Unset: no vhost is generated, and the servers are reachable on loopback only.
# WARNING: every <user>.$TRILIUM_DOMAIN needs a DNS record before it resolves.
TRILIUM_DOMAIN="${TRILIUM_DOMAIN:-}"
AWM_ENV=awm AWM_SERVICES="$PUBLIC_SERVICES" bash awm/gateway/install.sh

step "state symlinks"
for d in .awm data projects tasks main; do
    target=$STATE_ROOT/${d#.awm}; [ "$d" = .awm ] && target=$STATE_ROOT/state
    if [ -e "$d" ] && [ ! -L "$d" ]; then
        echo "refusing: $INSTALL_ROOT/$d is a real directory, expected a symlink to $target" >&2; exit 1
    fi
    [ "$(readlink "$d" 2>/dev/null)" = "$target" ] || ln -sfn "$target" "$d"
done

step "init as $APP_USER"
sudo -u "$APP_USER" env AWM_WORKSPACE=$INSTALL_ROOT HOME=$STATE_ROOT XDG_CONFIG_HOME=$STATE_ROOT/config \
    "$MF/envs/awm/bin/python" -m awm.gateway gateway init
ENABLED=$STATE_ROOT/state/services/enabled.json
if ! sudo test -f "$ENABLED"; then
    sudo -u "$APP_USER" install -d -m 750 "$(dirname "$ENABLED")"
    {
        echo "{"
        sep=""
        for d in awm/services/*/; do
            n=$(basename "$d")
            v=false; [ -n "$PUBLIC_SERVICES" ] && grep -qw -- "$n" <<<"$PUBLIC_SERVICES" && v=true
            printf '%s  "%s": %s' "$sep" "$n" "$v"; sep=$',\n'
        done
        echo; echo "}"
    } | sudo -u "$APP_USER" tee "$ENABLED" >/dev/null
    echo "   seeded $ENABLED (public service set)"
elif [ -n "$PUBLIC_SERVICES" ]; then
    # The seed above runs once. Adding a service to the public set on a box that
    # already has the file has to switch it on here, or the install lands, the
    # deploy reports success, and the service is never started.
    #
    # Additive on purpose: it only ever sets names in PUBLIC_SERVICES to true.
    # Anything switched on by hand since the seed stays on — this file is edited
    # from the box as well as from here.
    sudo -u "$APP_USER" "$MF/envs/awm/bin/python" \
        "$HERE/enable-services.py" "$ENABLED" $PUBLIC_SERVICES
fi

step "trilium: per-user scopes"
# A person exists to the service because a directory exists. On a mesh node that
# directory is a git worktree of the `userdata` project; this box holds no GitHub
# credential and cannot have one, so it is a plain directory and the service is
# told to accept that (TRILIUM_REQUIRE_SCOPE_GIT=0 below).
#
# CAUTION: that means snapshots and exports taken here are written and never
# committed. Their history lives on the node that can hold a checkout.
for u in $TRILIUM_USERS; do
    for sub in live data/backups notes; do
        sudo -u "$APP_USER" install -d -m 700 "$STATE_ROOT/projects/userdata/trilium/$u/$sub"
    done
    echo "   $u -> $STATE_ROOT/projects/userdata/trilium/$u"
done

# Two settings this box needs and no mesh node does. The unit reads them from
# /etc/awm/env, which lives outside the repo because it also holds secrets.
AWM_ENV_FILE=/etc/awm/env
sudo install -d -m 755 /etc/awm
sudo touch "$AWM_ENV_FILE"
for kv in TRILIUM_FRONTS=0 TRILIUM_REQUIRE_SCOPE_GIT=0; do
    if ! sudo grep -qx -- "$kv" "$AWM_ENV_FILE"; then
        # Drop any previous value for the key before appending. systemd takes the
        # last assignment, so a stale line above a new one is harmless and a
        # stale line below it silently wins.
        sudo sed -i "/^${kv%%=*}=/d" "$AWM_ENV_FILE"
        echo "$kv" | sudo tee -a "$AWM_ENV_FILE" >/dev/null
        echo "   $AWM_ENV_FILE += $kv"
    fi
done

step "trilium: nginx vhosts"
if [ -z "$TRILIUM_DOMAIN" ]; then
    echo "   TRILIUM_DOMAIN unset — no vhost. The servers answer on loopback only."
else
    TMPCONF="$(mktemp)"
    AWM_BIN="$MF/envs/awm/bin/awm" bash "$HERE/trilium-nginx.sh" "$TRILIUM_DOMAIN" "$TMPCONF"
    sudo install -m 644 "$TMPCONF" /etc/nginx/sites-available/trilium.conf
    sudo ln -sfn /etc/nginx/sites-available/trilium.conf /etc/nginx/sites-enabled/trilium.conf
    rm -f "$TMPCONF"
    # `nginx -t` before the reload. A bad config reloaded leaves the box with no
    # web server at all, and this is the only vhost the public reaches.
    sudo nginx -t && sudo systemctl reload nginx
    for u in $TRILIUM_USERS; do echo "   https://$u.$TRILIUM_DOMAIN"; done
    echo "   each name needs its own DNS record (or one wildcard)."
fi

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
