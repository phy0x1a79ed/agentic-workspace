#!/usr/bin/env bash
# V0: end-to-end validation of the awm federation stack, run from "bare"
# against the live "xaw" peer over the LAN/SSH tunnels.
#
# Pre-requisites already in place (set up incrementally during the build):
#   - ~/.awm/auth.token, ~/.awm/tls/cert.pem on both peers
#   - awm peer registry on both sides has the other as a registered peer
#   - SSH forward localhost:7821 → xaw:7820  (bare -> xaw direction)
#   - SSH reverse-forward xaw:localhost:7821 → bare's 7820 (xaw -> bare)
#   - bare awm-exposed running; xaw awm.service + awm-exposed.service running
#   - Both pip-installed editable from /home/tony/awm-src (xaw) and
#     /home/tony/agentic_workspace/projects/awm/remote-api (bare)
#
# This script:
#   - confirms source parity, peer pings, message round-trip both directions,
#     audit-log peer_id, degraded path, recovery, federated skill search

set -u
PASS=0; FAIL=0
check() {
    local label="$1"; shift
    local actual="$("$@" 2>&1)" ; local code=$?
    if [[ $code -eq 0 ]]; then
        echo "  ok: $label"
        PASS=$((PASS+1))
    else
        echo "  FAIL: $label"
        echo "    cmd: $*"
        echo "    out: $actual"
        FAIL=$((FAIL+1))
    fi
}
expect_in() {
    local label="$1"; local needle="$2"; shift 2
    local actual="$("$@" 2>&1)"
    if echo "$actual" | grep -q -- "$needle"; then
        echo "  ok: $label  (matched '$needle')"
        PASS=$((PASS+1))
    else
        echo "  FAIL: $label"
        echo "    cmd:    $*"
        echo "    expect: contains '$needle'"
        echo "    out:    $actual" | head -3
        FAIL=$((FAIL+1))
    fi
}

echo "=== 1. Source parity ==="
LOCAL_VER=$(awm --version 2>/dev/null || echo "0.1.0")
XAW_VER=$(ssh xaw '~/.local/bin/awm --version 2>/dev/null || echo "0.1.0"')
echo "  bare: $LOCAL_VER  /  xaw: $XAW_VER"

echo ""
echo "=== 2. /peer endpoint echoes ==="
BARE_TOKEN=$(cat ~/.awm/auth.token)
expect_in "bare /peer echoes dev-bare" "dev-bare" \
    curl -k -s -H "Authorization: Bearer $BARE_TOKEN" https://127.0.0.1:7820/peer
expect_in "xaw /peer echoes dev-xaw" "dev-xaw" \
    bash -c "curl -k -s -H 'Authorization: Bearer $(ssh xaw cat ~/.awm/auth.token)' https://localhost:7821/peer"

echo ""
echo "=== 3. Peer ping both directions ==="
expect_in "bare→xaw ping ok" '"ok": true' \
    awm peer ping dev-xaw
expect_in "xaw→bare ping ok" '"ok": true' \
    ssh xaw '~/.local/bin/awm peer ping dev-bare'

echo ""
echo "=== 4. Message round-trip ==="
SUBJ="v0-$(date +%s)"
expect_in "bare→xaw send succeeded" '"message": "Message forwarded' \
    awm inbox send "scope:awm/remote-api@dev-xaw" --sender opal --subject "${SUBJ}-bare" --body "from bare"
expect_in "xaw received w/ peer:dev-bare prefix" "peer:dev-bare/opal" \
    bash -c "ssh xaw '~/.local/bin/awm inbox fetch scope:awm/remote-api --limit 10' | python3 -c \"import json,sys; d=json.load(sys.stdin); print([m['sender'] for m in d['messages'] if '${SUBJ}-bare' in m['subject']])\""

expect_in "xaw→bare send succeeded" '"message": "Message forwarded' \
    ssh xaw "~/.local/bin/awm inbox send 'scope:awm/remote-api@dev-bare' --sender opal --subject '${SUBJ}-xaw' --body 'from xaw'"
expect_in "bare received w/ peer:dev-xaw prefix" "peer:dev-xaw/opal" \
    bash -c "awm inbox fetch scope:awm/remote-api --limit 10 | python3 -c \"import json,sys; d=json.load(sys.stdin); print([m['sender'] for m in d['messages'] if '${SUBJ}-xaw' in m['subject']])\""

echo ""
echo "=== 5. Negative cases ==="
expect_in "unknown peer rejected" "unknown peer: nonsense" \
    awm inbox send "scope:awm/remote-api@nonsense" --sender opal --subject x --body y
expect_in "fake X-Awm-From rejected" "unknown peer in X-Awm-From" \
    bash -c "curl -k -s -H 'Authorization: Bearer $BARE_TOKEN' -H 'X-Awm-From: fakepeer' -H 'Content-Type: application/json' -d '{\"scope\":\"workspace\",\"sender\":\"x\",\"msg_type\":\"notification\",\"subject\":\"s\",\"body\":\"b\"}' -X POST https://127.0.0.1:7820/inbox"

echo ""
echo "=== 6. Audit log peer_id on xaw ==="
expect_in "xaw access.log has peer_id:dev-bare" '"peer_id":"dev-bare"' \
    ssh xaw 'tail -5 ~/agentic_workspace/.awm/access.log'

echo ""
echo "=== 7. Federated skill search ==="
expect_in "skill search --peer dev-xaw returns origin tag" "dev-xaw" \
    awm skill search "agent" --peer dev-xaw

echo ""
echo "=== 8. Degraded path ==="
ssh xaw 'systemctl --user stop awm-exposed.service'
sleep 2
expect_in "skill search degraded gracefully" "degraded peers" \
    bash -c "awm skill search 'agent' --peer dev-xaw 2>&1"
ssh xaw 'systemctl --user start awm-exposed.service'
sleep 4
expect_in "skill search recovered" "dev-xaw" \
    awm skill search "agent" --peer dev-xaw

echo ""
echo "=== summary: $PASS passed, $FAIL failed ==="
exit $FAIL
