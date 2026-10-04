#!/bin/bash
# Seat role: join the host world as a real multiplayer player.
#
# A seat is a full Factorio client with nowhere to draw, so it brings its own
# display: Xvfb plus Mesa's llvmpipe software rasterizer. Once the join has
# settled the X window is unmapped, which (with halt-rendering-when-minimized,
# see config/config.ini) stops frame rendering entirely and drops the seat from
# ~1.5 cores to ~0.14. Vision is unaffected: take_screenshot renders offscreen
# at its own resolution and keeps working on an unmapped window.
set -euo pipefail

: "${SEAT_NAME:?SEAT_NAME is required (the in-game player name)}"
: "${FACTORIO_HOST:?FACTORIO_HOST is required (host or host:port of the server)}"

PORT="${FACTORIO_PORT:-12140}"
[[ "$FACTORIO_HOST" == *:* ]] && ADDRESS="$FACTORIO_HOST" || ADDRESS="${FACTORIO_HOST}:${PORT}"

export DISPLAY=":${SEAT_DISPLAY:-99}"
RESOLUTION="${SEAT_RESOLUTION:-1280x720}"
# The engine's own log, watched to learn when the join has actually completed.
GAME_LOG="${SEAT_LOG:-/opt/factorio/factorio-current.log}"
# Seconds to settle after the client reports InGame, before rendering is cut.
UNMAP_DELAY="${SEAT_UNMAP_DELAY:-2}"

# The in-game player name is read from the client's own player-data.json. The
# server sets require_user_verification=false, so no factorio.com account is
# needed for a seat -- the name is simply asserted, which is what lets the
# service bind a seat to game.players[SEAT_NAME] rather than guess which new
# player appeared.
python3 - "$SEAT_NAME" <<'PY'
import json, os, sys
path = "/opt/factorio/player-data.json"
data = {}
if os.path.exists(path):
    try:
        with open(path) as fh:
            data = json.load(fh)
    except Exception:
        data = {}
data["service-username"] = sys.argv[1]
with open(path, "w") as fh:
    json.dump(data, fh, indent=2)
PY

Xvfb "$DISPLAY" -screen 0 "${RESOLUTION}x24" -nolisten tcp &
for _ in $(seq 100); do
    xdpyinfo >/dev/null 2>&1 && break
    sleep 0.1
done
xdpyinfo >/dev/null 2>&1 || { echo "seat: Xvfb never came up on $DISPLAY" >&2; exit 69; }

# Unmapping must wait for the client to actually be in the game. Unmapping at
# the menu does NOT stop rendering once the world loads -- measured at ~3.8
# cores against 0.14 for a seat unmapped after the join -- because the engine
# latches its minimized state rather than polling the window, and re-reads it
# when the render target is rebuilt for the world. So the trigger is the
# engine's own "to(InGame)" transition in its log, not a timer.
if [[ "${SEAT_UNMAP:-1}" != "0" ]]; then
    (
        for _ in $(seq 1200); do
            [[ -f "$GAME_LOG" ]] && grep -q "to(InGame)" "$GAME_LOG" && break || true
            sleep 0.5
        done
        if ! grep -q "to(InGame)" "$GAME_LOG" 2>/dev/null; then
            echo "seat: never reached InGame; leaving rendering on" >&2
            exit 0
        fi
        sleep "$UNMAP_DELAY"
        win=$(xdotool search --name '^Factorio' 2>/dev/null | head -1) || true
        if [[ -z "${win:-}" ]]; then
            echo "seat: no Factorio window to unmap; leaving rendering on" >&2
            exit 0
        fi
        xdotool windowunmap "$win"
        echo "seat: window $win unmapped (rendering halted)"
        # Re-assert: a reconnect can map the window again, and a mapped window
        # renders flat out here (Xvfb reports 0 Hz, so nothing paces it).
        while sleep 30; do
            state=$(xwininfo -id "$win" 2>/dev/null | grep -c "IsViewable") || true
            [[ "${state:-0}" != "0" ]] && xdotool windowunmap "$win" || true
        done
    ) &
fi

echo "seat: joining ${ADDRESS} as ${SEAT_NAME}"
exec /opt/factorio/bin/x64/factorio \
    --mp-connect "$ADDRESS" \
    --mod-directory /opt/factorio/mods \
    "$@"
