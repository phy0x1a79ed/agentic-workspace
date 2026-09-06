#!/usr/bin/env bash
# rlm_harness.sh — exercise rlm-factorio against a throwaway, isolated gateway.
#
# Boots a fresh gateway whose discovery root (AWM_SERVICES_DIR) is a temp tree
# symlinking ONLY this service, and whose workspace root (AWM_WORKSPACE) is a
# temp dir, so it never touches prod state, DBs, or ports. The gateway
# auto-bootstraps the discovered service (default-enabled), which self-registers
# over the control WS; we then drive it over HTTP at /svc/rlm-factorio/fn/<verb>.
#
# The compose project is isolated too, and that is not cosmetic: the DEFAULT
# project owns `rlm-factorio_factorio-saves`, the volume the user's real worlds
# live in. This script generates worlds and tears volumes down with `-v`, so it
# must never run there. AWM_FACTORIO_PROJECT below is what keeps it away.
#
# Modes:
#   (none)     registration + verb-routing smoke — NO Docker.
#   --docker   the live lifecycle: a world, THREE seats playing in it at once,
#              a screenshot each, a blueprint, and the three ways a seat is
#              reclaimed (container killed underneath it, owner gone quiet,
#              service stopped). Slow on the first run (the image build
#              downloads Factorio); ~12 min after that, most of it deliberate
#              waiting on the reaper.
#
# Usage:  bash scratchpad/rlm_harness.sh [--docker]
set -uo pipefail

MODE="${1:-}"
SERVICE_DIR="$(cd "$(dirname "$0")/.." && pwd)"   # awm/services/rlm-factorio
PORT="${AWM_PORT:-7860}"
HUB="http://127.0.0.1:${PORT}"
COMPOSE="$SERVICE_DIR/appliance/docker-compose.yml"
PROJECT="rlm-factorio-harness"

HARNESS="$(mktemp -d "${TMPDIR:-/tmp}/rlm-factorio-harness.XXXXXX")"
mkdir -p "$HARNESS/services"
ln -s "$SERVICE_DIR" "$HARNESS/services/rlm-factorio"
: > "$HARNESS/AGENTS.md"   # workspace-root anchor (config also honors AWM_WORKSPACE)

export AWM_WORKSPACE="$HARNESS"
export AWM_SERVICES_DIR="$HARNESS/services"
export AWM_PORT="$PORT"
# The service ships a committed `profiles = ["gamebot"]` marker, so a gateway
# without that profile skips it at bootstrap. Claim it here.
export AWM_PROFILES="gamebot"
# Off the default project and ports — see the note at the top of this file.
export AWM_FACTORIO_PROJECT="$PROJECT"
export AWM_FACTORIO_CONTAINER="$PROJECT-appliance"
export AWM_FACTORIO_GAME_PORT=12240
export AWM_FACTORIO_CONTROL_PORT=12242
# Sweep often, and age a seat out in 90s rather than an hour, so the reaper's
# real path fits inside a test's patience.
export AWM_FACTORIO_REAP_POLL_S=5
export AWM_FACTORIO_SEAT_IDLE_S=90

GW_LOG="$HARNESS/gateway.log"
SVC_LOG="$HARNESS/.awm/logs/services/rlm-factorio.log"
PASS=0 FAIL=0

say()  { printf '\n=== %s ===\n' "$*"; }
ok()   { PASS=$((PASS+1)); printf '  ok   %s\n' "$*"; }
bad()  { FAIL=$((FAIL+1)); printf '  FAIL %s\n' "$*"; }

# POST a verb. Sets globals RESP_BODY / RESP_CODE; returns 0 iff HTTP 2xx.
# (Default body set on its own line — `${2:-{}}` mis-parses the braces in bash.)
invoke() {
    local fn="$1" data="${2:-}" r
    [ -n "$data" ] || data='{}'
    r="$(curl -s -w '|%{http_code}' --max-time 1900 -X POST \
            "$HUB/svc/rlm-factorio/fn/$fn" \
            -H 'content-type: application/json' -d "$data")"
    RESP_CODE="${r##*|}"; RESP_BODY="${r%|*}"
    case "$RESP_CODE" in 2*) return 0 ;; *) return 1 ;; esac
}

# Extract a dotted path from a JSON string. Usage: jget "$RESP_BODY" snapshot.position.x
# Prints empty + returns nonzero on any miss, so callers can guard with [ -n ... ].
jget() {
    printf '%s' "$1" | python3 -c '
import sys, json
try:
    d = json.load(sys.stdin)
    for k in sys.argv[1].split("."):
        d = d[int(k)] if k.lstrip("-").isdigit() else d[k]
    print(d)
except Exception:
    sys.exit(1)
' "$2" 2>/dev/null
}

# One field of one seat, by seat id. Usage: seatf <seat_id> <field>
seatf() {
    invoke seats "{\"session_id\":\"$SID\"}" >/dev/null 2>&1
    printf '%s' "$RESP_BODY" | python3 -c '
import sys, json
d = json.load(sys.stdin)
print(next((str(s.get(sys.argv[2], "")) for s in d["seats"]
            if s["seat_id"] == sys.argv[1]), "MISSING"))
' "$1" "$2" 2>/dev/null
}

# Wait until a routed verb answers again (used after the service is restarted).
wait_routable() {
    for _ in $(seq 1 120); do
        invoke status '{}' && return 0
        sleep 1
    done
    return 1
}

cleanup() {
    say "teardown"
    # Kill the whole gateway process group (gateway + any service it spawned).
    if [ -n "${GW_PGID:-}" ]; then kill -TERM "-$GW_PGID" 2>/dev/null || true; fi
    sleep 2
    if [ -n "${GW_PGID:-}" ]; then kill -KILL "-$GW_PGID" 2>/dev/null || true; fi
    # Leave nothing of this rig behind. `-v` is safe ONLY because the project is
    # the harness's own — on the default project it would take the real saves.
    docker ps -a --format '{{.Names}}' | grep "^$PROJECT" \
        | xargs -r docker rm -f -v >/dev/null 2>&1 || true
    docker compose -p "$PROJECT" -f "$COMPOSE" down -v >/dev/null 2>&1 || true
    rm -rf "$HARNESS"
    printf '\n=== summary: %d passed, %d failed ===\n' "$PASS" "$FAIL"
    [ "$FAIL" -eq 0 ] || exit 1
}
trap cleanup EXIT INT TERM

say "booting throwaway gateway on :$PORT (workspace=$HARNESS)"
setsid bash -c 'exec mamba run -n awm --no-capture-output python -m awm.gateway gateway serve' \
    >"$GW_LOG" 2>&1 &
GW_PGID=$!

# Wait for the gateway HTTP to answer (/ is unrouted → 404; poll a real route).
up=0
for _ in $(seq 1 80); do
    code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 "$HUB/hub/services" 2>/dev/null || true)"
    if [ "$code" = "200" ]; then up=1; break; fi
    sleep 0.5
done
[ "$up" = 1 ] && ok "gateway up" || { bad "gateway did not come up; log:"; tail -30 "$GW_LOG"; exit 1; }

# Confirm discovery, then gate on a real routed call (the service appears in
# /hub/services before its control WS is routable — a status 200 proves both).
say "waiting for rlm-factorio to register"
# Poll rather than check once: the gateway answers /hub/services as soon as it
# is up, which is before the service it just spawned has registered itself.
seen=0
for _ in $(seq 1 60); do
    if curl -fsS --max-time 3 "$HUB/hub/services" 2>/dev/null | grep -q '"rlm-factorio"'; then
        seen=1; break
    fi
    sleep 1
done
if [ "$seen" = 1 ]; then
    ok "service discovered"
else
    bad "service not in /hub/services; service log:"; tail -40 "$SVC_LOG" 2>/dev/null
    bad "gateway log:"; tail -20 "$GW_LOG"; exit 1
fi
wait_routable && ok "control channel routable" \
    || { bad "service never became routable (last $RESP_CODE: $RESP_BODY)"; tail -40 "$GW_LOG"; exit 1; }

# --- verb-routing smoke (no Docker) --------------------------------------
say "verb-routing smoke (no Docker)"
case "$RESP_BODY" in
    *'"sessions"'*) ok "status returns a sessions list" ;;
    *) bad "status payload unexpected: $RESP_BODY" ;;
esac
invoke observe '{"session_id":"nope"}' || true
case "$RESP_BODY" in
    *unknown\ session*) ok "observe rejects an unknown session" ;;
    *) bad "observe did not reject bad session ($RESP_CODE): $RESP_BODY" ;;
esac
invoke mine '{"session_id":"nope","x":0,"y":0}' || true
case "$RESP_BODY" in
    *unknown\ session*) ok "mine routes + rejects an unknown session" ;;
    *) bad "mine did not route ($RESP_CODE): $RESP_BODY" ;;
esac
# Addressing is the contract every act verb shares: name a seat, or a session
# that holds exactly one. Naming neither is a usage error, never a default —
# picking for the caller would act as somebody else's character.
invoke move '{"x":0,"y":0}' || true
case "$RESP_BODY" in
    *"pass seat_id, or session_id"*) ok "an unaddressed act verb is refused" ;;
    *) bad "unaddressed move was not refused ($RESP_CODE): $RESP_BODY" ;;
esac
invoke observe '{"seat_id":"seat-nope"}' || true
case "$RESP_BODY" in
    *unknown\ seat_id*) ok "observe rejects an unknown seat" ;;
    *) bad "observe did not reject bad seat ($RESP_CODE): $RESP_BODY" ;;
esac

if [ "$MODE" != "--docker" ]; then
    say "no-Docker smoke complete (pass --docker for the live lifecycle)"
    exit 0
fi

# --- live lifecycle (Docker) ---------------------------------------------
say "live lifecycle (Docker) — first acquire builds the image, be patient"
if invoke acquire '{"game":"factorio"}'; then
    SID="$(jget "$RESP_BODY" session_id)"
    [ -n "$SID" ] && ok "acquire -> $SID" || { bad "acquire returned no session_id: $RESP_BODY"; exit 1; }
else
    bad "acquire failed ($RESP_CODE): $RESP_BODY"; tail -60 "$GW_LOG"; exit 1
fi

if docker compose -p "$PROJECT" -f "$COMPOSE" ps --status running -q | grep -q .; then
    ok "appliance container running"
else
    bad "no running appliance container"
fi

# The mod binds at map generation, so a world older than the mod has no storage
# and every seat verb errors. A fresh world is the documented first move.
invoke world_new "{\"session_id\":\"$SID\",\"seed\":424242}" \
    && ok "world_new seed=424242" || bad "world_new failed ($RESP_CODE): $RESP_BODY"

# --- three seats in one world --------------------------------------------
say "seating three players in one world"
# The first joins through /invoke carrying X-Awm-As, which is how a placed
# agent's identity reaches a service and the ONLY way to prove the seat records
# its caller: nothing else in this script has an identity to send.
S1="$(curl -s --max-time 900 -X POST "$HUB/invoke" \
        -H 'content-type: application/json' -H 'X-Awm-As: unit:agent-alpha' \
        -d "{\"name\":\"rlm_factorio_join\",\"args\":{\"session_id\":\"$SID\"}}" \
     | python3 -c 'import sys,json
r = json.load(sys.stdin)["result"]
print((json.loads(r) if isinstance(r, str) else r)["seat_id"])' 2>/dev/null)"
[ -n "$S1" ] && ok "joined over /invoke as unit:agent-alpha -> $S1" \
             || { bad "join through /invoke failed"; exit 1; }

SEATS="$S1"
for who in beta gamma; do
    if invoke join "{\"session_id\":\"$SID\",\"owner\":\"$who\"}"; then
        s="$(jget "$RESP_BODY" seat_id)"
        [ -n "$s" ] && { SEATS="$SEATS $s"; ok "$who -> $s as $(jget "$RESP_BODY" player_name)"; } \
                    || bad "join $who returned no seat_id: $RESP_BODY"
    else
        bad "join $who failed ($RESP_CODE): $RESP_BODY"
    fi
    # Look in on everyone already seated. A join takes about a minute, so three
    # of them outlast the deliberately short idle clock set above and the first
    # seat would age out before anybody had moved -- which is the reaper working
    # correctly, not a bug, but it is not what this section is testing. An
    # orchestrator watching its players do this anyway.
    for held in $SEATS; do invoke observe "{\"seat_id\":\"$held\"}" >/dev/null 2>&1; done
done
set -- $SEATS
S1="${1:-}"; S2="${2:-}"; S3="${3:-}"
[ -n "$S3" ] || { bad "fewer than three seats joined; stopping"; exit 1; }

invoke seats "{\"session_id\":\"$SID\"}" || true
NCONN="$(printf '%s' "$RESP_BODY" | python3 -c \
    'import sys,json;print(sum(1 for s in json.load(sys.stdin)["seats"] if s.get("connected")))' 2>/dev/null)"
[ "$NCONN" = 3 ] && ok "three players connected at once" \
    || bad "expected 3 connected players, saw $NCONN: $RESP_BODY"
NIDX="$(printf '%s' "$RESP_BODY" | python3 -c \
    'import sys,json;d=json.load(sys.stdin);print(len({s["player_index"] for s in d["seats"]}))' 2>/dev/null)"
[ "$NIDX" = 3 ] && ok "three distinct in-game players" || bad "player indexes not distinct: $RESP_BODY"
OWNERS="$(printf '%s' "$RESP_BODY" | python3 -c \
    'import sys,json;d=json.load(sys.stdin);print(",".join(sorted(s["owner"] for s in d["seats"])))' 2>/dev/null)"
[ "$OWNERS" = "beta,gamma,unit:agent-alpha" ] \
    && ok "each seat records its owner (caller identity, or an explicit one)" \
    || bad "owners wrong: $OWNERS"

# With three seats the session no longer names one player, and the service says
# so rather than picking.
invoke observe "{\"session_id\":\"$SID\"}" || true
case "$RESP_BODY" in
    *"pass seat_id ("*) ok "a session with three seats refuses to be addressed alone" ;;
    *) bad "ambiguous session was not refused ($RESP_CODE): $RESP_BODY" ;;
esac

say "each seat walks somewhere different"
i=0
for s in $S1 $S2 $S3; do
    i=$((i+1)); tx=$((i*12))
    invoke move "{\"seat_id\":\"$s\",\"x\":$tx,\"y\":0}" \
        && ok "seat $i heading for x=$tx" || bad "move $s failed: $RESP_BODY"
done
sleep 25
POSN="$(for s in $S1 $S2 $S3; do
    invoke observe "{\"seat_id\":\"$s\"}" >/dev/null 2>&1
    jget "$RESP_BODY" snapshot.position.x
done | sort -u | grep -c .)"
[ "$POSN" = 3 ] && ok "three players at three different positions" \
    || bad "seats did not move independently ($POSN distinct x)"

say "each seat sees the world for itself"
SHOTS=0
for s in $S1 $S2 $S3; do
    invoke screenshot "{\"seat_id\":\"$s\",\"width\":480,\"height\":320,\"zoom\":0.5}" || true
    f="$(jget "$RESP_BODY" path)"
    [ -n "$f" ] && [ -s "$f" ] && SHOTS=$((SHOTS+1))
done
[ "$SHOTS" = 3 ] && ok "a PNG on disk per seat" || bad "only $SHOTS of 3 screenshots landed"

say "blueprint round trip through a seat"
# Put something in front of the seat to capture. Placed straight through
# exec_lua rather than through `build`, because what is under test here is the
# capture, not whether freeplay happened to hand this player a furnace. Passed
# as a FILE, which is both the honest way to hand Lua to this verb and the only
# way a multi-line script survives the trip through shell quoting and JSON.
cat > "$HARNESS/plant.lua" <<'LUA'
local c = player.character
local e = c.surface.create_entity{ name = "stone-furnace",
                                   position = { c.position.x + 3,
                                                c.position.y + 3 },
                                   force = player.force }
rcon.print(e and "placed" or "no")
LUA
invoke exec_lua "{\"seat_id\":\"$S1\",\"path\":\"$HARNESS/plant.lua\"}" || true
[ "$(jget "$RESP_BODY" output)" = "placed" ] && ok "planted an entity beside the seat (exec_lua from a file)" \
    || bad "could not place a test entity ($RESP_CODE): $RESP_BODY"
invoke blueprint_capture "{\"seat_id\":\"$S1\",\"radius\":8}" || true
BP="$(jget "$RESP_BODY" path)"
[ -n "$BP" ] && [ -s "$BP" ] && ok "captured a blueprint to a file" \
    || bad "blueprint_capture produced no file ($RESP_CODE): $RESP_BODY"
# And back in as ghosts on the shared force. Stamped near the seat on purpose:
# ungenerated map reports every entity blocked, and only the ground a player has
# actually been near is guaranteed generated.
invoke observe "{\"seat_id\":\"$S1\"}" || true
TX="$(python3 -c "print(round(float('$(jget "$RESP_BODY" snapshot.position.x)')))")"
TY="$(python3 -c "print(round(float('$(jget "$RESP_BODY" snapshot.position.y)')) + 12)")"
invoke blueprint_stamp "{\"seat_id\":\"$S1\",\"path\":\"$BP\",\"x\":$TX,\"y\":$TY,\"clear\":true}" || true
GH="$(jget "$RESP_BODY" placed)"
[ -n "$GH" ] && [ "$GH" -ge 1 ] 2>/dev/null && ok "stamped it back as $GH ghost(s)" \
    || bad "blueprint_stamp placed nothing ($RESP_CODE): $RESP_BODY"

# --- the reaper ----------------------------------------------------------
say "kill a seat's container out from under the service"
C3="$(seatf "$S3" container_name)"
docker rm -f -v "$C3" >/dev/null 2>&1 && ok "removed $C3" || bad "could not remove $C3"
REAPED=""
for i in $(seq 1 12); do
    # Seats 1 and 2 are held only by being used, and the idle threshold here is
    # 90s, so this loop doubles as the proof that a heartbeat holds a seat.
    invoke observe "{\"seat_id\":\"$S1\"}" >/dev/null 2>&1
    invoke observe "{\"seat_id\":\"$S2\"}" >/dev/null 2>&1
    [ -z "$REAPED" ] && [ "$(seatf "$S3" status)" = "stopped" ] && REAPED="$i"
    sleep 10
done
[ -n "$REAPED" ] && ok "reaper reclaimed the dead seat within $((REAPED*10))s" \
    || bad "dead seat still $(seatf "$S3" status) after 120s"
[ "$(seatf "$S1" status)" = "ready" ] && [ "$(seatf "$S2" status)" = "ready" ] \
    && ok "seats in use survived 120s of a 90s idle threshold" \
    || bad "a seat in use was reaped ($S1=$(seatf "$S1" status) $S2=$(seatf "$S2" status))"

say "a seat left alone ages out (S2, ~110s)"
sleep 110
[ "$(seatf "$S2" status)" = "stopped" ] && ok "idle seat reclaimed" \
    || bad "idle seat still $(seatf "$S2" status)"
# S1 was quiet for the same 110s, so it should have aged out too: proof the
# reaper is judging each seat's own clock and not the sweep's.
[ "$(seatf "$S1" status)" = "stopped" ] && ok "so did the other quiet seat" \
    || bad "S1 unexpectedly still $(seatf "$S1" status)"

say "SIGTERM: a stopped service must not leave a client running"
invoke join "{\"session_id\":\"$SID\",\"owner\":\"shutdown-probe\"}" || true
S4="$(jget "$RESP_BODY" seat_id)"
[ -n "$S4" ] && ok "joined $S4" || bad "join for the shutdown probe failed: $RESP_BODY"
C4="$(seatf "$S4" container_name)"
SVC_PID="$(pgrep -f 'awm.rlm_factorio.hub_adapter' | head -1)"
[ -n "$SVC_PID" ] && ok "service pid $SVC_PID" || bad "service pid not found"
kill -TERM "$SVC_PID" 2>/dev/null
for _ in $(seq 1 30); do
    docker ps --format '{{.Names}}' | grep -q "^$C4$" || break
    sleep 1
done
docker ps --format '{{.Names}}' | grep -q "^$C4$" \
    && bad "SIGTERM left seat container $C4 running" \
    || ok "SIGTERM reclaimed the seat container"
docker ps --format '{{.Names}}' | grep -q "^$AWM_FACTORIO_CONTAINER$" \
    && ok "the world's own container survived the service stopping" \
    || bad "SIGTERM took the appliance down with it"

# --- teardown ------------------------------------------------------------
say "release"
wait_routable && ok "service came back after SIGTERM" || bad "service did not come back"
invoke release "{\"session_id\":\"$SID\"}" \
    && ok "release" || bad "release failed ($RESP_CODE): $RESP_BODY"
if docker compose -p "$PROJECT" -f "$COMPOSE" ps --status running -q | grep -q .; then
    bad "container still running after release"
else
    ok "container gone after release"
fi
if docker volume ls --format '{{.Name}}' | grep -q "^${PROJECT}_factorio-saves$"; then
    ok "saves volume survived release"
else
    bad "saves volume missing after release"
fi
say "what the reaper logged"
grep 'reap: reclaimed\|shutdown: reclaimed' "$SVC_LOG" 2>/dev/null | tail -8
