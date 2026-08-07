#!/usr/bin/env bash
# Runs ON mira (not the awm host). Installs the headless Slack/Opera clients +
# the creds extractor as user-level systemd services. Idempotent.
#
# Deploy from the awm host with:
#   rsync -a awm/services/social/mira/ mira:awm-social-mira/
#   ssh mira 'bash ~/awm-social-mira/install-mira.sh'
#
# Prereqs (installed once, by hand): snap package `opera`, apt
# `x11vnc openbox scrot`, and `loginctl enable-linger tony` so the user manager
# runs headless.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SHARE="$HOME/.local/share/awm-social-mira"
BIN="$HOME/.local/bin"
UNITS="$HOME/.config/systemd/user"

echo "== helper venv (websocket-client for the extractor, aiohttp for the daemon) =="
mkdir -p "$SHARE" "$BIN" "$UNITS"
if [ ! -x "$SHARE/venv/bin/python" ]; then
    python3 -m venv "$SHARE/venv"
fi
"$SHARE/venv/bin/pip" install -q --upgrade pip websocket-client aiohttp
cp "$HERE/awm-slack-creds.py" "$SHARE/awm-slack-creds.py"

echo "== mira API daemon package ($SHARE/mira_api) =="
rm -rf "$SHARE/mira_api"
cp -r "$HERE/mira_api" "$SHARE/mira_api"

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
# Retired units. Copying is a glob, so a unit only stops being installed once it
# is removed here as well as from systemd/ — otherwise the old file lingers and
# an already-enabled copy keeps running. awm-slack: the Slack *desktop* app,
# replaced by the app.slack.com tab in Opera. Electron's single-instance lock
# made every launch exit 0 into the incumbent, which Restart=always then read as
# a crash — 442k restarts before it was caught.
for retired in awm-slack.service; do
    if [ -e "$UNITS/$retired" ] || systemctl --user is-enabled "$retired" >/dev/null 2>&1; then
        echo "   retiring $retired"
        systemctl --user disable --now "$retired" >/dev/null 2>&1 || true
        rm -f "$UNITS/$retired"
        systemctl --user reset-failed "$retired" >/dev/null 2>&1 || true
    fi
done
systemctl --user daemon-reload
# Display + WM + clients run always; VNC is on-demand only (not enabled).
systemctl --user enable --now awm-display.service
systemctl --user enable --now awm-wm.service
systemctl --user enable --now awm-opera-teams.service
# The API daemon needs the awm bearer token + TLS certs in ~/.awm; it serves on
# 172.16.0.24:7822 over the awm-network. Started last (Opera must be up first).
systemctl --user enable --now awm-mira-api.service

echo "== status =="
systemctl --user --no-pager --plain list-units 'awm-*' || true
echo
echo "Next: one-time login. Start the VNC bridge and tunnel in from your laptop:"
echo "  ssh mira 'systemctl --user start awm-vnc'"
echo "  ssh -N -L 5920:127.0.0.1:5920 mira      # from the laptop"
echo "  <VNC client> -> localhost:5920          # sign in to Slack (and Teams)"
echo "  ssh mira 'systemctl --user stop awm-vnc' # when done"
echo "Verify creds:  ssh mira $BIN/awm-slack-creds | python3 -c 'import sys,json;d=json.load(sys.stdin);print(d[\"team\"], d[\"url\"], \"token+cookie OK\")'"
