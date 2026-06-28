#!/bin/sh
# Proton VPN dialer. Logs in, connects, then stays in the foreground polling the
# tunnel so the kill-switch can fail closed (see supervisord.conf). Reads env
# injected by the vpn service:
#   PROTON_USERNAME, PROTON_PASSWORD  — Proton account (or OpenVPN) credentials
#   PROTON_SERVER                     — optional connect target; empty = --fastest
#
# NOTE: protonvpn-cli's login/connect/status surface differs across versions.
# This is the common community-CLI shape; if your image's CLI differs, adjust
# the three invocations below (see INSTALL.md § Proton).
set -eu

echo "[dial] logging in to Proton as $PROTON_USERNAME"
# Non-interactive login: feed the password on stdin to `login <username>`.
printf '%s\n' "$PROTON_PASSWORD" | protonvpn-cli login "$PROTON_USERNAME" || \
    echo "[dial] WARN: login returned non-zero (may already be logged in)"

echo "[dial] connecting${PROTON_SERVER:+ to $PROTON_SERVER}"
if [ -n "${PROTON_SERVER:-}" ]; then
    protonvpn-cli connect "$PROTON_SERVER"
else
    protonvpn-cli connect --fastest
fi

# Second belt: Proton's own killswitch (best-effort; ignore if unsupported).
protonvpn-cli killswitch --on 2>/dev/null || true

# Foreground keepalive — fail closed the instant the tunnel reports down.
echo "[dial] connected; monitoring tunnel"
while protonvpn-cli status 2>/dev/null | grep -qiE 'connected|active'; do
    sleep 10
done
echo "[dial] proton tunnel down — exiting (fail-closed)"
exit 1
