#!/usr/bin/env bash
# The clean install (install.md R118, the install half): a bare container with no conda and no
# Python runs every core plugin from this tree's own build. Run it only when asked; it is never
# part of the fast smoke.
#
# It builds nothing. $AWM_TEST_BINS holds the static awm and one static binary per core plugin.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
repo="$(cd "$here/../.." && pwd)"
. "$repo/scripts/pins.sh"
bins="${AWM_TEST_BINS:-$("$repo/scripts/rust-build.sh" --target-dir)/x86_64-unknown-linux-musl/release}"
image="awm-test-node:clean"
run="awm-clean-$$"
work="$(mktemp -d)"

cleanup() {
    docker rm -f "$run" >/dev/null 2>&1 || true
    rm -rf "$work"
}
trap cleanup EXIT

mapfile -t plugins < <(core_plugins "$repo")
for name in awm "${plugins[@]}"; do
    [ -x "$bins/$name" ] || { echo "no static $name in $bins: build the workspace for musl or set AWM_TEST_BINS" >&2; exit 2; }
done

docker build -q -t "$image" "$here/images/node" >/dev/null

mkdir -p "$work/.awm/bin" "$work/projects"
git -C "$repo" archive HEAD AGENTS.md core/plugins | tar -x -C "$work"
for name in "${plugins[@]}"; do cp "$bins/$name" "$work/.awm/bin/$name"; done

cat > "$work/.awm/config.toml" <<EOF
node = "clean-node"
listen = "127.0.0.1:7819"
workspace = "/ws"
core_roots = ["/ws/core/plugins"]
EOF
# Throwaway values: a plugin with a declared secret starts only when the secret is set.
sed -n 's/^secrets = \[\(.*\)\].*/\1/p' "$work"/core/plugins/*/awm-plugin.toml | tr -d '" ' | tr ',' '\n' | sort -u |
    while read -r secret; do [ -z "$secret" ] || echo "$secret=$(openssl rand -hex 32)"; done > "$work/.awm/secrets.env"

docker run -d --name "$run" --network none --user "$(id -u):$(id -g)" -e HOME=/tmp -e AWM_DIR=/ws/.awm \
    -v "$bins/awm:/usr/local/bin/awm:ro" -v "$work:/ws" "$image" \
    sh -c 'exec awm gateway serve --state /ws/.awm 2>/ws/.awm/gateway.log' >/dev/null

docker exec "$run" sh -c '! command -v python3 && ! command -v python && ! command -v conda && ! command -v mamba' \
    || { echo "FAIL: the container is not bare" >&2; exit 1; }

awm_in() { docker exec -e AWM_GATEWAY_URL=http://127.0.0.1:7819 "$run" awm "$@"; }
waiting='[.[] | select(.enabled and .status != "ready")]'
for _ in $(seq 120); do
    table="$(awm_in services 2>/dev/null)" && [ "$(jq "$waiting | length" <<<"$table")" = 0 ] && break
    sleep 1
done

failed=""
jq -e "$waiting | length == 0" <<<"${table:-[]}" >/dev/null || failed="not every service reached ready"
loaded="$(awm_in plugins)"
jq -e '.refused | length == 0' <<<"$loaded" >/dev/null || failed="a plugin was refused"
for name in "${plugins[@]}"; do
    jq -e --arg n "$name" 'any(.[]; .plugin == $n and .status == "ready")' <<<"$table" >/dev/null || failed="$name is not ready"
done

if [ -n "$failed" ]; then
    echo "FAIL: $failed" >&2
    jq -r '.[] | [.name, .status, (.refusal // "")] | @tsv' <<<"${table:-[]}" >&2
    jq -c '.refused' <<<"$loaded" >&2
    tail -30 "$work/.awm/gateway.log" >&2
    exit 1
fi
printf 'PASS: %d core plugins ready with no conda and no Python\n' "${#plugins[@]}"
