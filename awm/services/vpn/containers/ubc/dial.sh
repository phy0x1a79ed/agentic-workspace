#!/bin/sh
# UBC openconnect dialer. Runs in the foreground under supervisor; when
# openconnect exits, the kill-switch tears the container down (see
# supervisord.conf). Reads everything from env injected by the vpn service:
#   UBC_SERVER, UBC_USER, UBC_PASSWORD   — primary credentials
#   UBC_SECOND_FACTOR                    — Duo factor line (default "push")
#   VA_BURST_URL, VA_TOKEN               — virtual-auth Duo auto-approver (optional)
set -eu

# MTU tune to avoid fragmentation on the Cisco ASA endpoint (vpn_bounce lesson).
ip link set dev eth0 mtu 1400 2>/dev/null || true

# --- 2FA: arm the on-demand Duo auto-approver on mira BEFORE dialing ----------
# virtual-auth watches the CWL Duo account and approves the first pending login
# within ~1s. We fire the burst, then immediately dial so the push we trigger is
# the one it approves. Best-effort: if no URL is configured, skip and rely on
# 2FA being otherwise satisfied. (One login per burst — the vpn service
# serializes UBC `up` so two dials never race the single approval.)
if [ -n "${VA_BURST_URL:-}" ]; then
    echo "[dial] arming virtual-auth Duo burst at $VA_BURST_URL"
    if ! curl -fsS --max-time 15 -X POST "$VA_BURST_URL" \
            -H "X-Token: ${VA_TOKEN:-}" >/dev/null; then
        echo "[dial] WARN: virtual-auth burst request failed — dial may hang on Duo"
    fi
fi

# --- dial ---------------------------------------------------------------------
# openconnect reads successive password prompts from stdin lines: the primary
# password, then the second-factor line ("push") which triggers the Duo push.
echo "[dial] connecting to $UBC_SERVER as $UBC_USER"
printf '%s\n%s\n' "$UBC_PASSWORD" "${UBC_SECOND_FACTOR:-push}" \
    | openconnect "$UBC_SERVER" --user="$UBC_USER" --passwd-on-stdin
