#!/usr/bin/env bash
# V0: end-to-end validation of the awm federation stack, run from "capella"
# against the live "crux" peer over the LAN/SSH tunnels.
#
# Pre-requisites already in place (set up incrementally during the build):
#   - ~/.awm/auth.token, ~/.awm/tls/cert.pem on both peers
#   - awm peer registry on both sides has the other as a registered peer
#   - SSH forward localhost:7821 → crux:7820  (capella -> xaw direction)
#   - SSH reverse-forward crux:localhost:7821 → capella's 7820 (crux -> bare)
#   - capella awm-exposed running; crux awm.service + awm-exposed.service running
#   - Both pip-installed editable from /home/tony/awm-src (crux) and
#     /home/tony/agentic_workspace/projects/awm/remote-api (capella)
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
CAPELLA_VER=$(awm --version 2>/dev/null || echo "0.1.0")
CRUX_VER=$(ssh crux '~/.local/bin/awm --version 2>/dev/null || echo "0.1.0"')
echo "  capella: $CAPELLA_VER  /  crux: $CRUX_VER"

echo ""
echo "=== 2. /peer endpoint echoes ==="
BARE_TOKEN=$(cat ~/.awm/auth.token)
expect_in "capella /peer echoes capella" "capella" \
    curl -k -s -H "Authorization: Bearer $BARE_TOKEN" https://127.0.0.1:7820/peer
expect_in "crux /peer echoes crux" "crux" \
    bash -c "curl -k -s -H 'Authorization: Bearer $(ssh crux cat ~/.awm/auth.token)' https://localhost:7821/peer"

echo ""
echo "=== 3. Peer ping both directions ==="
expect_in "capella→crux ping ok" '"ok": true' \
    awm peer ping crux
expect_in "crux→capella ping ok" '"ok": true' \
    ssh crux '~/.local/bin/awm peer ping capella'

echo ""
echo "=== 4. Message round-trip ==="
SUBJ="v0-$(date +%s)"
expect_in "capella→crux send succeeded" '"message": "Message forwarded' \
    awm inbox send "scope:awm/remote-api@crux" --sender opal --subject "${SUBJ}-bare" --body "from capella"
expect_in "xaw received w/ peer:capella prefix" "peer:capella/opal" \
    bash -c "ssh crux '~/.local/bin/awm inbox fetch scope:awm/remote-api --limit 10' | python3 -c \"import json,sys; d=json.load(sys.stdin); print([m['sender'] for m in d['messages'] if '${SUBJ}-bare' in m['subject']])\""

expect_in "crux→capella send succeeded" '"message": "Message forwarded' \
    ssh crux "~/.local/bin/awm inbox send 'scope:awm/remote-api@capella' --sender opal --subject '${SUBJ}-xaw' --body 'from xaw'"
expect_in "bare received w/ peer:crux prefix" "peer:crux/opal" \
    bash -c "awm inbox fetch scope:awm/remote-api --limit 10 | python3 -c \"import json,sys; d=json.load(sys.stdin); print([m['sender'] for m in d['messages'] if '${SUBJ}-xaw' in m['subject']])\""

echo ""
echo "=== 5. Negative cases ==="
expect_in "unknown peer rejected" "unknown peer: nonsense" \
    awm inbox send "scope:awm/remote-api@nonsense" --sender opal --subject x --body y
expect_in "fake X-Awm-From rejected" "unknown peer in X-Awm-From" \
    bash -c "curl -k -s -H 'Authorization: Bearer $BARE_TOKEN' -H 'X-Awm-From: fakepeer' -H 'Content-Type: application/json' -d '{\"scope\":\"workspace\",\"sender\":\"x\",\"msg_type\":\"notification\",\"subject\":\"s\",\"body\":\"b\"}' -X POST https://127.0.0.1:7820/inbox"

echo ""
echo "=== 6. Audit log peer_id on xaw ==="
expect_in "xaw access.log has peer_id:capella" '"peer_id":"capella"' \
    ssh crux 'tail -5 ~/agentic_workspace/.awm/access.log'

echo ""
echo "=== 7. Federated skill search ==="
expect_in "skill search --peer crux returns origin tag" "crux" \
    awm skill search "agent" --peer crux

echo ""
echo "=== 8. Degraded path ==="
ssh crux 'systemctl --user stop awm-exposed.service'
sleep 2
expect_in "skill search degraded gracefully" "degraded peers" \
    bash -c "awm skill search 'agent' --peer crux 2>&1"
ssh crux 'systemctl --user start awm-exposed.service'
sleep 4
expect_in "skill search recovered" "crux" \
    awm skill search "agent" --peer crux

echo ""
echo "=== summary: $PASS passed, $FAIL failed ==="
exit $FAIL
