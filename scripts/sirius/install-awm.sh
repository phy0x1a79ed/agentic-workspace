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
#   4. `awm gateway init` as the app user, enabled.json (all off for now)
#   5. /usr/local/bin/{awm,awm-mcp}, awm.service, restart
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
# The service set installed and enabled on this box. Empty: the gateway
# alone. Every other service under awm/services/ is written as disabled.
PUBLIC_SERVICES=""
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
