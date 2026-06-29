#!/usr/bin/env bash
# rlm_harness.sh — exercise rlm-factorio against a throwaway, isolated gateway.
#
# Boots a fresh gateway whose discovery root (AWM_SERVICES_DIR) is a temp tree
# symlinking ONLY this service, and whose workspace root (AWM_WORKSPACE) is a
# temp dir, so it never touches prod/dev state, DBs, or ports. The gateway
# auto-bootstraps the discovered service (default-enabled), which self-registers
# over the control WS; we then drive it over HTTP at /svc/rlm-factorio/fn/<verb>.
#
# Modes:
#   (none)     registration + verb-routing smoke — NO Docker. Asserts the
#              service is discovered+running and that status/observe route.
#   --docker   full live lifecycle: acquire (builds + boots the appliance),
#              world_save -> world_load -> world_new, then release. Slow on the
#              first run (image build downloads Factorio).
#
# Usage:  bash scratchpad/rlm_harness.sh [--docker]
set -uo pipefail

MODE="${1:-}"
SERVICE_DIR="$(cd "$(dirname "$0")/.." && pwd)"   # awm/services/rlm-factorio
PORT="${AWM_PORT:-7860}"
HUB="http://127.0.0.1:${PORT}"
COMPOSE="$SERVICE_DIR/appliance/docker-compose.yml"

HARNESS="$(mktemp -d "${TMPDIR:-/tmp}/rlm-factorio-harness.XXXXXX")"
mkdir -p "$HARNESS/services"
ln -s "$SERVICE_DIR" "$HARNESS/services/rlm-factorio"
: > "$HARNESS/AGENTS.md"   # workspace-root anchor (config also honors AWM_WORKSPACE)

export AWM_WORKSPACE="$HARNESS"
export AWM_SERVICES_DIR="$HARNESS/services"
export AWM_PORT="$PORT"

GW_LOG="$HARNESS/gateway.log"
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

cleanup() {
    say "teardown"
    # Kill the whole gateway process group (gateway + any service it spawned).
    if [ -n "${GW_PGID:-}" ]; then kill -TERM "-$GW_PGID" 2>/dev/null || true; fi
    sleep 1
    if [ -n "${GW_PGID:-}" ]; then kill -KILL "-$GW_PGID" 2>/dev/null || true; fi
    # Ensure no appliance container is left behind (keeps the saves volume).
    docker compose -p rlm-factorio -f "$COMPOSE" down >/dev/null 2>&1 || true
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
if curl -fsS --max-time 3 "$HUB/hub/services" 2>/dev/null | grep -q '"rlm-factorio"'; then
    ok "service discovered"
else
    bad "service not in /hub/services; gateway log:"; tail -40 "$GW_LOG"; exit 1
fi
ready=0
for _ in $(seq 1 60); do
    if invoke status '{}'; then ready=1; break; fi
    sleep 0.5
done
[ "$ready" = 1 ] && ok "control channel routable (status -> $RESP_BODY)" \
    || { bad "service never became routable (last $RESP_CODE: $RESP_BODY)"; tail -40 "$GW_LOG"; exit 1; }

# --- verb-routing smoke (no Docker) --------------------------------------
say "verb-routing smoke (no Docker)"
case "$RESP_BODY" in
    *'"sessions"'*) ok "status returns a sessions list" ;;
    *) bad "status payload unexpected: $RESP_BODY" ;;
esac
invoke observe '{"session_id":"nope"}' || true
case "$RESP_BODY" in
    *unknown\ session*) ok "observe rejects bad session ($RESP_CODE): $RESP_BODY" ;;
    *) bad "observe did not reject bad session ($RESP_CODE): $RESP_BODY" ;;
esac

if [ "$MODE" != "--docker" ]; then
    say "no-Docker smoke complete (pass --docker for the live lifecycle)"
    exit 0
fi

# --- live lifecycle (Docker) ---------------------------------------------
say "live lifecycle (Docker) — first acquire builds the image, be patient"
if invoke acquire '{"game":"factorio"}'; then
    SID="$(printf '%s' "$RESP_BODY" | python3 -c 'import sys,json; print(json.load(sys.stdin)["session_id"])' 2>/dev/null || true)"
    [ -n "$SID" ] && ok "acquire -> $SID" || { bad "acquire returned no session_id: $RESP_BODY"; exit 1; }
else
    bad "acquire failed ($RESP_CODE): $RESP_BODY"; tail -60 "$GW_LOG"; exit 1
fi

# Container should be running and the supervisor ready.
if docker compose -p rlm-factorio -f "$COMPOSE" ps --status running -q | grep -q .; then
    ok "appliance container running"
else
    bad "no running appliance container"
fi

# Body play needs a world whose map-gen ran the game-bot-control mod's on_init.
# The persistent saves volume may hold a pre-mod _active.zip, so generate a
# fresh world first (the documented "first run must be world_new").
say "player-body lifecycle (spawn -> move -> observe -> save/load)"
invoke world_new "{\"session_id\":\"$SID\",\"seed\":424242}" \
    && ok "world_new seed=424242 -> $RESP_BODY" || bad "world_new failed ($RESP_CODE): $RESP_BODY"

invoke body_spawn "{\"session_id\":\"$SID\",\"x\":0,\"y\":0}"
if [ "$RESP_CODE" = 200 ] && [ -n "$(jget "$RESP_BODY" position.x)" ]; then
    ok "body_spawn -> $RESP_BODY"
else
    bad "body_spawn failed ($RESP_CODE): $RESP_BODY"
fi

invoke observe "{\"session_id\":\"$SID\"}" || true
if [ "$(jget "$RESP_BODY" snapshot.body)" = "True" ] && [ -n "$(jget "$RESP_BODY" snapshot.health)" ]; then
    ok "observe sees a live body (health=$(jget "$RESP_BODY" snapshot.health)) -> $RESP_BODY"
else
    bad "observe did not see a spawned body ($RESP_CODE): $RESP_BODY"
fi

# One-shot move + poll: the body walks toward x=20 then stops (walking:false).
invoke body_move "{\"session_id\":\"$SID\",\"x\":20,\"y\":0}" \
    && ok "body_move -> $RESP_BODY" || bad "body_move failed ($RESP_CODE): $RESP_BODY"
say "polling observe while the body walks toward x=20"
moved=0 last_x=""
for _ in $(seq 1 30); do
    invoke observe "{\"session_id\":\"$SID\"}" || true
    x="$(jget "$RESP_BODY" snapshot.position.x)"
    w="$(jget "$RESP_BODY" snapshot.walking)"
    [ -n "$x" ] && last_x="$x"
    if [ "$w" = "False" ] && [ -n "$x" ] \
       && python3 -c "import sys; sys.exit(0 if abs(float('$x')-20)<1.0 else 1)" 2>/dev/null; then
        moved=1; break
    fi
    sleep 1
done
[ "$moved" = 1 ] && ok "body walked to x≈$last_x and stopped" \
    || bad "body did not converge on x=20 (last x=$last_x)"

# Persistence: the body lives in the mod's storage, so it survives save/load.
invoke world_save "{\"session_id\":\"$SID\",\"name\":\"bodytest\",\"overwrite\":true}" \
    && ok "world_save bodytest -> $RESP_BODY" || bad "world_save failed ($RESP_CODE): $RESP_BODY"
invoke world_load "{\"session_id\":\"$SID\",\"name\":\"bodytest\"}" \
    && ok "world_load bodytest -> $RESP_BODY" || bad "world_load failed ($RESP_CODE): $RESP_BODY"
invoke observe "{\"session_id\":\"$SID\"}" || true
if [ "$(jget "$RESP_BODY" snapshot.body)" = "True" ]; then
    ok "body persisted across save/load -> $RESP_BODY"
else
    bad "body lost across save/load ($RESP_CODE): $RESP_BODY"
fi

invoke release "{\"session_id\":\"$SID\"}" \
    && ok "release -> $RESP_BODY" || bad "release failed ($RESP_CODE): $RESP_BODY"
if docker compose -p rlm-factorio -f "$COMPOSE" ps --status running -q | grep -q .; then
    bad "container still running after release"
else
    ok "container gone after release"
fi
if docker volume ls --format '{{.Name}}' | grep -q 'rlm-factorio.*saves'; then
    ok "saves volume survived release"
else
    bad "saves volume missing after release"
fi
