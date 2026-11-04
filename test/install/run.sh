#!/usr/bin/env bash
# The install test (install.md R118): a bare container installs awm, then joins a fake
# fleet as a new node. It also reloads a changed plugin hot (R144) and calls through the
# isolated door (fleet.md R109 item 7). Run it only when asked; it is never part of the fast smoke.
#
# It builds nothing. It takes the static awm binary at $AWM_TEST_BIN and the images that
# build-images.sh made (or that the registry holds). It touches no real login or node.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
repo="$(cd "$here/../.." && pwd)"
reg="${AWM_TEST_REGISTRY:-quay.io/hallamlab}"
tag="${AWM_TEST_TAG:-dev}"
bin="${AWM_TEST_BIN:-$repo/core/target/x86_64-unknown-linux-musl/release/awm}"
node_img="$reg/awm-test-node:$tag"
stub_img="$reg/awm-test-stubs:$tag"
run="awm-install-$$"
work="$(mktemp -d)"
user=(--user "$(id -u):$(id -g)" -e HOME=/tmp)

[ -x "$bin" ] || { echo "no static awm at $bin: build it or set AWM_TEST_BIN" >&2; exit 2; }
for img in "$node_img" "$stub_img"; do
    docker image inspect "$img" >/dev/null 2>&1 || docker pull "$img" >/dev/null \
        || { echo "no image $img: run $here/build-images.sh" >&2; exit 2; }
done

cleanup() {
    docker rm -f "$run-peer" "$run-node" >/dev/null 2>&1 || true
    docker network rm "$run" >/dev/null 2>&1 || true
    rm -rf "$work"
}
trap cleanup EXIT

step() { printf '\n== %s\n' "$*"; }
fail() { echo "FAIL: $*" >&2; for c in peer node; do echo "--- $c gateway log" >&2; docker exec "$run-$c" sh -c 'cat /state/gateway.log 2>/dev/null' >&2 || true; done; exit 1; }
awm_in() { local c="$1"; shift; docker exec -e AWM_GATEWAY_URL=http://127.0.0.1:7819 "$run-$c" awm "$@"; }
wait_up() {
    for _ in $(seq 100); do
        awm_in "$1" status >/dev/null 2>&1 && return 0
        sleep 0.1
    done
    fail "the $1 gateway did not come up"
}

mkdir -p "$work/peer" "$work/node" "$work/ca" "$work/src"
docker network create "$run" >/dev/null

step "a throwaway certificate authority"
docker run --rm "${user[@]}" -v "$work/ca:/ca" "$node_img" sh -ec '
    openssl req -x509 -newkey ed25519 -nodes -days 1 -subj "/CN=awm-install-test-ca" -keyout /ca/ca.key -out /ca/ca.pem 2>/dev/null
    for n in fake-peer new-node; do
        openssl req -newkey ed25519 -nodes -subj "/CN=$n" -keyout /ca/$n.key -out /ca/$n.csr 2>/dev/null
        openssl x509 -req -in /ca/$n.csr -CA /ca/ca.pem -CAkey /ca/ca.key -CAcreateserial -days 1 -out /ca/$n.pem 2>/dev/null
        openssl verify -CAfile /ca/ca.pem /ca/$n.pem
    done'

step "the fake peer serves stub 2fa and social"
cat > "$work/peer/config.toml" <<EOF
node = "fake-peer"
listen = "0.0.0.0:7819"
core_roots = ["/opt/awm-test/plugins/peer"]
[[peer]]
name = "new-node"
url = "http://new-node:7819"
EOF
docker run -d --name "$run-peer" "${user[@]}" --network "$run" --network-alias fake-peer \
    -v "$bin:/usr/local/bin/awm:ro" -v "$work/peer:/state" "$stub_img" \
    sh -c 'exec awm gateway serve --state /state 2>/state/gateway.log' >/dev/null
wait_up peer

step "a bare container installs awm"
docker run -d --name "$run-node" "${user[@]}" --network "$run" --network-alias new-node \
    -v "$bin:/usr/local/bin/awm:ro" -v "$work/node:/state" -v "$work/src:/src:ro" "$node_img" sleep infinity >/dev/null
docker exec "$run-node" sh -c 'command -v awm >/dev/null && ! ls /opt/awm-test 2>/dev/null' || fail "the node is not bare"
cat > "$work/node.toml" <<EOF
node = "new-node"
listen = "0.0.0.0:7819"
isolated_listen = "127.0.0.1:7829"
[[peer]]
name = "fake-peer"
url = "http://fake-peer:7819"
EOF
docker cp "$work/node.toml" "$run-node:/tmp/install-config.toml"
docker exec "$run-node" awm bootstrap --state /state --config /tmp/install-config.toml
docker exec -d "$run-node" sh -c 'exec awm gateway serve --state /state 2>/state/gateway.log'
wait_up node

step "the new node sees the fleet's tools by name"
table="$(awm_in node awm table)"
for t in 2fa social; do
    echo "$table" | grep -q "\"name\": \"$t\"" || fail "the node's table lacks $t: $table"
done
echo "$table" | grep -q '"peer": "fake-peer"' || fail "the node's table names no peer: $table"
out="$(awm_in node 2fa echo --text "via the fleet")" || fail "2fa from the new node: $out"
echo "$out" | grep -q "via the fleet" || fail "2fa answered $out"

step "the new node installs a plugin from a folder; its needs resolve on the peer"
docker create --name "$run-src" "$stub_img" true >/dev/null
docker cp "$run-src:/opt/awm-test/plugins/install/hello" "$work/src/hello"
docker cp "$run-src:/opt/awm-test/plugins/install/cch" "$work/src/cch"
cp "$here/door.sh" "$work/src/door.sh"
docker rm "$run-src" >/dev/null
awm_in node install /src/hello || fail "install hello"
out="$(awm_in node hello call_needed --verb echo --args '{"text":"hello needs 2fa"}')" || fail "hello's call: $out"
echo "$out" | grep -q "hello needs 2fa" || fail "hello's call to 2fa answered $out"
docker exec "$run-node" grep -q 'name = "hello"' /state/installed.toml || fail "no pin for hello"

step "the peer hears the new node's table change"
for _ in $(seq 50); do
    awm_in peer awm table | grep -q '"name": "hello"' && break
    sleep 0.1
done
awm_in peer awm table | grep -q '"name": "hello"' || fail "the peer never learned hello"
out="$(awm_in peer hello echo --text "from the peer")" || fail "hello from the peer: $out"
echo "$out" | grep -q "from the peer" || fail "hello from the peer answered $out"

step "a changed plugin reloads hot, with no gateway restart (R144)"
core_pid() { awm_in node status | grep -o '"core_pid": [0-9]*'; }
before="$(core_pid)"
docker exec "$run-node" sh -c 'echo "# changed" >> /state/installed/hello/run.sh'
out="$(awm_in node reload)" || fail "reload: $out"
echo "$out" | tr -d ' \n' | grep -q '"updated":\["hello"\]' || fail "the reload did not update hello: $out"
out="$(awm_in node hello echo --text "after the reload")" || fail "hello after the reload: $out"
echo "$out" | grep -q "after the reload" || fail "hello after the reload answered $out"
[ "$(core_pid)" = "$before" ] || fail "the reload restarted the gateway"

step "an isolated session calls through the door with its caller token (R109 item 7)"
awm_in node install /src/cch >/dev/null || fail "install the stub harness"
door() { docker exec "$run-node" bash /src/door.sh "$@"; }
rpc() { printf '{"jsonrpc":"2.0","id":1,"method":"%s","params":%s}' "$1" "$2"; }
door - "$(rpc initialize '{}')" | head -1 | grep -q " 401 " || fail "the door let in a call with no token"
door guess "$(rpc initialize '{}')" | head -1 | grep -q " 401 " || fail "the door let in an unknown token"
out="$(door good "$(rpc initialize '{"protocolVersion":"2025-06-18","capabilities":{}}')")"
echo "$out" | grep -q '"serverInfo"' || fail "initialize through the door: $out"
out="$(door good "$(rpc tools/call '{"name":"hello","arguments":{"verb":"echo","args":{"text":"from the box"}}}')")"
echo "$out" | grep -q "from the box" || fail "hello echo through the door: $out"
out="$(door good "$(rpc tools/call '{"name":"hello","arguments":{"verb":"whoami"}}')")"
echo "$out" | grep -q 'untrusted' || fail "the door's caller is not the untrusted session: $out"
out="$(door good "$(rpc tools/call '{"name":"hello","arguments":{"verb":"add","args":{"a":1,"b":2}}}')")"
echo "$out" | grep -q 'whitelist' || fail "the door ran a verb off the token's whitelist: $out"
awm_in node uninstall cch >/dev/null || fail "uninstall the stub harness"

step "uninstall runs hot on both nodes"
awm_in node uninstall hello >/dev/null || fail "uninstall hello"
for _ in $(seq 50); do
    awm_in peer awm table | grep -q '"name": "hello"' || break
    sleep 0.1
done
! awm_in peer awm table | grep -q '"name": "hello"' || fail "the peer still lists hello"

printf '\nPASS: the install test\n'
