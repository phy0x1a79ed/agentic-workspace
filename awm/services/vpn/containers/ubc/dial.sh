#!/bin/sh
# UBC openconnect dialer. Runs in the foreground under supervisor; when
# openconnect exits, the kill-switch tears the container down (see
# supervisord.conf). Reads everything from env injected by the vpn service:
#   UBC_SERVER, UBC_USER, UBC_PASSWORD   — primary credentials
#   UBC_SECOND_FACTOR                    — Duo factor line (default "push")
#
# The Duo push this dial triggers is auto-approved by the local awm `2fa`
# service: the host-side `vpn_up` arms a `2fa_burst device=<twofa_device>` on the
# gateway right before we start (see container.py). No in-container 2FA call —
# the container can't reach the host gateway before the tunnel is up.
set -eu

# MTU tune to avoid fragmentation on the Cisco ASA endpoint (vpn_bounce lesson).
ip link set dev eth0 mtu 1400 2>/dev/null || true

# --- dial ---------------------------------------------------------------------
# openconnect reads successive password prompts from stdin lines: the primary
# password, then the second-factor line ("push") which triggers the Duo push.
echo "[dial] connecting to $UBC_SERVER as $UBC_USER"
printf '%s\n%s\n' "$UBC_PASSWORD" "${UBC_SECOND_FACTOR:-push}" \
    | openconnect "$UBC_SERVER" --user="$UBC_USER" --passwd-on-stdin
