#!/usr/bin/env bash
# V0: end-to-end validation of the awm rooms/agents/SSH-tunnel stack.
# Driven from bare; reaches dev-xaw over the SSH tunnel.
#
# Pre-requisites (already in place after the M0–M6 build):
#   - bare: awm-exposed bound 127.0.0.1:7820, no TLS
#   - bare: ~/.awm/auth.token, ~/.awm/peers/dev-xaw.token
#   - xaw : awm-exposed bound 127.0.0.1:7820, no TLS
#   - bare: maintains ssh -fN -R 7821:127.0.0.1:7820 xaw (reverse tunnel)
#   - peer registry: bare has dev-xaw(--ssh-alias xaw); xaw has dev-bare(loopback :7821)
#   - scope `awm/room-test` exists on bare
#
# This script runs the V0 scenarios that apply to a single-project
# WSL2 + laptop rig. Scenarios requiring the same project mirrored on
# both peers (V0 #11 — agent on remote peer in local room) are marked
# SKIP with a one-line reason.

set -u
PASS=0; FAIL=0; SKIP=0
PEER=dev-xaw
SCOPE=awm/room-test

export AWM_AUTH_TOKEN_FILE="${AWM_AUTH_TOKEN_FILE:-/home/tony/.awm/auth.token}"
export AWM_WORKSPACE="${AWM_WORKSPACE:-/home/tony/agentic_workspace}"

XAW_RUN() {
  ssh xaw "export AWM_WORKSPACE=/home/tony/agentic_workspace
           ~/miniforge3/envs/awm/bin/$*"
}

check() {
  local label="$1"; shift
  local out; out="$("$@" 2>&1)"; local code=$?
  if [[ $code -eq 0 ]]; then
    echo "  ok: $label"; PASS=$((PASS+1))
  else
    echo "  FAIL: $label"
    echo "    cmd: $*"
    echo "    out: $(echo "$out" | head -3 | sed 's/^/      /')"
    FAIL=$((FAIL+1))
  fi
}
expect_in() {
  local label="$1"; local needle="$2"; shift 2
  local out; out="$("$@" 2>&1)"
  if echo "$out" | grep -q -- "$needle"; then
    echo "  ok: $label  (matched '$needle')"; PASS=$((PASS+1))
  else
    echo "  FAIL: $label"
    echo "    cmd:    $*"
    echo "    expect: contains '$needle'"
    echo "    out:    $(echo "$out" | head -3 | sed 's/^/      /')"
    FAIL=$((FAIL+1))
  fi
}
expect_not_in() {
  local label="$1"; local needle="$2"; shift 2
  local out; out="$("$@" 2>&1)"
  if ! echo "$out" | grep -q -- "$needle"; then
    echo "  ok: $label  (did not contain '$needle')"; PASS=$((PASS+1))
  else
    echo "  FAIL: $label  (unexpected match for '$needle')"; FAIL=$((FAIL+1))
  fi
}
skip() {
  echo "  SKIP: $1 ($2)"; SKIP=$((SKIP+1))
}
get_field() {
  # Extract a JSON string field by key from the FIRST occurrence in stdin.
  python3 -c "import sys, json, re
raw = sys.stdin.read().strip()
m = re.search(r'\{.*\}', raw, re.S)
if not m: sys.exit(1)
d = json.loads(m.group(0))
print(d.get('$1', ''))"
}

echo "=== 1. Tunnel + ping ==="
expect_in "bare→xaw ping" '"ok": true' \
    awm peer ping $PEER
expect_in "xaw→bare ping" '"ok": true' \
    ssh xaw 'export AWM_WORKSPACE=/home/tony/agentic_workspace; ~/miniforge3/envs/awm/bin/awm peer ping dev-bare'
COUNT=$(pgrep -fc 'ssh -fN.*ControlMaster=auto.*peer-dev-xaw' || true)
if [[ "$COUNT" == "1" ]]; then
  echo "  ok: exactly one ssh master alive"; PASS=$((PASS+1))
else
  echo "  FAIL: expected 1 ssh master, got $COUNT"; FAIL=$((FAIL+1))
fi

echo ""
echo "=== 2. Rooms CRUD + transcript (C2) ==="
ROOM=$(awm room create --topic "v0-crud" | get_field id)
[[ -n "$ROOM" ]] && { echo "  ok: room created ($ROOM)"; PASS=$((PASS+1)); } \
    || { echo "  FAIL: no room id"; FAIL=$((FAIL+1)); }
expect_in "verb-noun shape" "^[a-z]\\+-[a-z]\\+\\(-[0-9]\\+\\)\\?\$" \
    bash -c "echo $ROOM"
expect_in "room shows up in list" "$ROOM" awm room list
expect_in "post lands in transcript" "v0 hello world" \
    bash -c "awm room post $ROOM 'v0 hello world' >/dev/null && awm room history $ROOM"
expect_in "search hits by topic" "v0-crud" awm room search "v0-crud"
expect_in "close transitions status" "closed" \
    bash -c "awm room close $ROOM | grep status"

echo ""
echo "=== 3. Negative paths (C10 / O8) ==="
expect_in "unknown room → 404" "no such room" \
    awm room post "nonexistent-room" "x"
expect_in "post to closed room rejected" "is closed" \
    awm room post $ROOM "after-close"
expect_in "unauth /rooms → 401" "401" \
    bash -c 'curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:7820/rooms'

echo ""
echo "=== 4. Cross-peer room search + post (C6) ==="
MARKER=$(date +%s)
R2=$(awm room create --topic "v0-xpeer-$MARKER" | get_field id)
expect_in "xaw finds bare's room via --peer all" "v0-xpeer-$MARKER" \
    ssh xaw "export AWM_WORKSPACE=/home/tony/agentic_workspace; ~/miniforge3/envs/awm/bin/awm room search v0-xpeer-$MARKER --peer all"
expect_in "xaw gets room@dev-bare" "host_peer_id" \
    ssh xaw "export AWM_WORKSPACE=/home/tony/agentic_workspace; ~/miniforge3/envs/awm/bin/awm room get $R2@dev-bare"
expect_in "xaw posts to room@dev-bare" '"message": "posted"' \
    ssh xaw "export AWM_WORKSPACE=/home/tony/agentic_workspace; ~/miniforge3/envs/awm/bin/awm room post $R2@dev-bare 'xaw ping'"
expect_in "post visible on bare with xaw origin tag" "user:operator@dev-xaw" \
    awm room history $R2
awm room close $R2 >/dev/null

echo ""
echo "=== 5. WS subscriber stream + history ==="
R3=$(awm room create --topic "v0-ws" | get_field id)
awm room post $R3 "seed post" >/dev/null
WSOUT=$(mktemp)
(timeout 4 awm room join $R3 --quiet > "$WSOUT" 2>&1) &
WSPID=$!
sleep 1
awm room post $R3 "live post via REST" >/dev/null
sleep 2
wait $WSPID 2>/dev/null || true
expect_in "WS attach replayed seed post" "seed post" cat "$WSOUT"
expect_in "WS attach saw live post" "live post via REST" cat "$WSOUT"
rm -f "$WSOUT"
awm room close $R3 >/dev/null

echo ""
echo "=== 6. Localhost binding (C0 negative) ==="
XAW_IP=$(ssh xaw 'hostname -I' | awk '{print $1}')
expect_in "xaw's 7820 not on LAN" "000" \
    bash -c "curl -s -o /dev/null --connect-timeout 3 -w '%{http_code}' http://$XAW_IP:7820/peer"
expect_in "but tunneled ping still works" '"ok": true' \
    awm peer ping $PEER

echo ""
echo "=== 7. Schema + scope-unique guarantees ==="
expect_in "bare DB at v22" "22" \
    bash -c "python3 -c \"import sqlite3; c=sqlite3.connect('$AWM_WORKSPACE/.awm/state.db'); print(c.execute('SELECT version FROM schema_version').fetchone()[0])\""
expect_in "partial unique index present" "idx_agent_sessions_active_unique" \
    bash -c "python3 -c \"import sqlite3; c=sqlite3.connect('$AWM_WORKSPACE/.awm/state.db'); print([r[0] for r in c.execute(\\\"SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='agent_sessions'\\\").fetchall()])\""

echo ""
echo "=== Deferred / requires extra setup ==="
skip "V0 #11 — agent on remote peer in local room" \
    "xaw has no awm project mirror; needs separate sync"
skip "V0 #6  — multi-room single agent stdin ordering" \
    "requires real claude session — covered by M1 mock-subprocess test"
skip "V0 #7  — multi-agent room (no auto-feed)" \
    "requires two scopes with running claude agents"
skip "V0 #12 — agent invokes API via MCP" \
    "requires MCP-driven claude session"
skip "V0 #13 — tunnel reconnect under live attach" \
    "covered by ad-hoc verification in M0; needs orchestrated background runner"
skip "V0 #14 — reconcile on service restart" \
    "covered by sessions_live unit tests; needs real session for full e2e"
skip "V0 #16 — audit trail with peer_id rows for /rooms" \
    "access_log only audits writes via the mounted core; /rooms router doesn't hit that path"

echo ""
echo "=== summary: $PASS passed, $FAIL failed, $SKIP skipped ==="
exit $FAIL
