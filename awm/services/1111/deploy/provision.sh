#!/bin/bash
# Install or update everything the 1111 service needs outside the awm tree.
# Run as root from any checkout: sudo awm/services/1111/deploy/provision.sh
# Idempotent. Restarts the viewer if it was running so it picks up new code.
set -euo pipefail

ACCOUNT=u1111
HOME_DIR="/home/$ACCOUNT"
SUDOERS=/etc/sudoers.d/awm-1111
HERE="$(cd "$(dirname "$0")" && pwd)"
VIEW_SRC="$HERE/../view"

[[ $EUID -eq 0 ]] || { echo "run as root" >&2; exit 1; }

if ! id "$ACCOUNT" >/dev/null 2>&1; then
    useradd -m -s /bin/bash "$ACCOUNT"
fi
chmod 700 "$HOME_DIR"

install -d -o "$ACCOUNT" -g "$ACCOUNT" -m 755 "$HOME_DIR/bin" "$HOME_DIR/run"
install -o "$ACCOUNT" -g "$ACCOUNT" -m 700 "$HERE/1111ctl.sh" "$HOME_DIR/bin/1111ctl.sh"
if [[ ! -f "$HOME_DIR/run/pip-constraints.txt" ]]; then
    echo 'setuptools<70' > "$HOME_DIR/run/pip-constraints.txt"
    chown "$ACCOUNT:$ACCOUNT" "$HOME_DIR/run/pip-constraints.txt"
fi

rsync -a --delete --exclude __pycache__ --chown="$ACCOUNT:$ACCOUNT" "$VIEW_SRC/" "$HOME_DIR/view/"
install -d -o "$ACCOUNT" -g "$ACCOUNT" -m 700 "$HOME_DIR/view-data"

install -m 440 "$HERE/awm-1111.sudoers" "$SUDOERS.tmp"
if visudo -cf "$SUDOERS.tmp"; then
    mv "$SUDOERS.tmp" "$SUDOERS"
else
    rm -f "$SUDOERS.tmp"
    echo "sudoers rule failed validation; $SUDOERS left unchanged" >&2
    exit 1
fi

if sudo -n -u "$ACCOUNT" "$HOME_DIR/bin/1111ctl.sh" view-status >/dev/null; then
    sudo -n -u "$ACCOUNT" "$HOME_DIR/bin/1111ctl.sh" view-restart
fi
[[ -d "$HOME_DIR/stable-diffusion-webui" ]] ||
    echo "note: no checkout at $HOME_DIR/stable-diffusion-webui yet" >&2
echo "provisioned"
