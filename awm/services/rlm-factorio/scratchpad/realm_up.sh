#!/usr/bin/env bash
# realm_up.sh — bring the Factorio realm up for real play, on the REAL world.
#
# The harness (rlm_harness.sh) is a throwaway that tears its world down with
# `-v`. This is its opposite: a long-lived gateway on the DEFAULT compose
# project, so the world it hosts is the one in `rlm-factorio_factorio-saves`
# and a person can join it from their own Factorio client on UDP 12140.
#
# It NEVER runs `docker compose down -v`. Stopping this script leaves the world
# container up on purpose — the appliance holds the world and is re-adopted.
#
# Usage:  bash scratchpad/realm_up.sh          # boot + wait
#         bash scratchpad/realm_up.sh --stop   # stop the gateway (world stays up)
set -uo pipefail

SERVICE_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PORT="${AWM_PORT:-7865}"
HUB="http://127.0.0.1:${PORT}"
RIG="${RLM_FACTORIO_RIG:-$HOME/.local/share/rlm-factorio-rig}"
PIDFILE="$RIG/gateway.pid"
GW_LOG="$RIG/gateway.log"

if [ "${1:-}" = "--stop" ]; then
    if [ -f "$PIDFILE" ]; then
        kill -TERM "-$(cat "$PIDFILE")" 2>/dev/null || true
        sleep 2; kill -KILL "-$(cat "$PIDFILE")" 2>/dev/null || true
        rm -f "$PIDFILE"
        echo "gateway stopped; world container left running (by design)"
    else
        echo "no pidfile at $PIDFILE"
    fi
    exit 0
fi

mkdir -p "$RIG/services"
ln -sfn "$SERVICE_DIR" "$RIG/services/rlm-factorio"
: > "$RIG/AGENTS.md"   # workspace-root anchor

export AWM_WORKSPACE="$RIG"
export AWM_SERVICES_DIR="$RIG/services"
export AWM_PORT="$PORT"
export AWM_PROFILES="gamebot"
# DEFAULT project on purpose: this is the real world, not a rig world.
export AWM_FACTORIO_PROJECT="rlm-factorio"
export AWM_FACTORIO_CONTAINER="rlm-factorio-appliance"
export AWM_FACTORIO_GAME_PORT=12140
export AWM_FACTORIO_CONTROL_PORT=12142

if curl -fsS --max-time 2 "$HUB/hub/services" >/dev/null 2>&1; then
    echo "gateway already up on :$PORT"
else
    setsid bash -c 'exec mamba run -n awm --no-capture-output python -m awm.gateway gateway serve' \
        >"$GW_LOG" 2>&1 &
    echo $! > "$PIDFILE"
    for _ in $(seq 1 120); do
        curl -fsS --max-time 3 "$HUB/hub/services" >/dev/null 2>&1 && break
        sleep 0.5
    done
    curl -fsS --max-time 3 "$HUB/hub/services" >/dev/null 2>&1 \
        || { echo "gateway did not come up:"; tail -30 "$GW_LOG"; exit 1; }
    echo "gateway up on :$PORT (workspace $RIG)"
fi

for _ in $(seq 1 90); do
    curl -fsS --max-time 3 "$HUB/hub/services" 2>/dev/null | grep -q '"rlm-factorio"' && break
    sleep 1
done
curl -fsS --max-time 3 "$HUB/hub/services" 2>/dev/null | grep -q '"rlm-factorio"' \
    && echo "rlm-factorio registered" \
    || { echo "service did not register:"; tail -40 "$RIG/.awm/logs/services/rlm-factorio.log" 2>/dev/null; exit 1; }

echo
echo "drive it with:   AWM_PORT=$PORT awm rlm factorio-status"
echo "or over HTTP:    curl -s -XPOST $HUB/svc/rlm-factorio/fn/status -d '{}' -H 'content-type: application/json'"
