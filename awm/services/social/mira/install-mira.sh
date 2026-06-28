#!/usr/bin/env bash
# Runs ON mira (not the awm host). Installs the headless Slack/Opera clients +
# the creds extractor as user-level systemd services. Idempotent.
#
# Deploy from the awm host with:
#   rsync -a awm/services/social/mira/ mira:awm-social-mira/
#   ssh mira 'bash ~/awm-social-mira/install-mira.sh'
#
# Prereqs (installed once, by hand): snap packages `slack` + `opera`, apt
# `x11vnc openbox scrot`, and `loginctl enable-linger tony` so the user manager
# runs headless.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SHARE="$HOME/.local/share/awm-social-mira"
BIN="$HOME/.local/bin"
UNITS="$HOME/.config/systemd/user"

echo "== helper venv (isolated; only websocket-client) =="
mkdir -p "$SHARE" "$BIN" "$UNITS"
if [ ! -x "$SHARE/venv/bin/python" ]; then
    python3 -m venv "$SHARE/venv"
fi
"$SHARE/venv/bin/pip" install -q --upgrade pip websocket-client
cp "$HERE/awm-slack-creds.py" "$SHARE/awm-slack-creds.py"

echo "== extractor wrapper ($BIN/awm-slack-creds) =="
cat > "$BIN/awm-slack-creds" <<EOF
#!/bin/sh
# Full path is used by the connector's creds_cmd: ~/.local/bin is NOT on the
# non-interactive ssh PATH.
exec "$SHARE/venv/bin/python" "$SHARE/awm-slack-creds.py" "\$@"
EOF
chmod +x "$BIN/awm-slack-creds"

echo "== systemd user units =="
cp "$HERE"/systemd/*.service "$UNITS/"
systemctl --user daemon-reload
# Display + WM + clients run always; VNC is on-demand only (not enabled).
systemctl --user enable --now awm-display.service
systemctl --user enable --now awm-wm.service
systemctl --user enable --now awm-slack.service
systemctl --user enable --now awm-opera-teams.service

echo "== status =="
systemctl --user --no-pager --plain list-units 'awm-*' || true
echo
echo "Next: one-time login. Start the VNC bridge and tunnel in from your laptop:"
echo "  ssh mira 'systemctl --user start awm-vnc'"
echo "  ssh -N -L 5920:127.0.0.1:5920 mira      # from the laptop"
echo "  <VNC client> -> localhost:5920          # sign in to Slack (and Teams)"
echo "  ssh mira 'systemctl --user stop awm-vnc' # when done"
echo "Verify creds:  ssh mira $BIN/awm-slack-creds | python3 -c 'import sys,json;d=json.load(sys.stdin);print(d[\"team\"], d[\"url\"], \"token+cookie OK\")'"
