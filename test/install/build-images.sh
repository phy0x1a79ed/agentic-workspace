#!/usr/bin/env bash
# Build the install test's images (install.md R119). Push them with AWM_TEST_PUSH=1.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
repo="$(cd "$here/../.." && pwd)"
reg="${AWM_TEST_REGISTRY:-quay.io/hallamlab}"
tag="${AWM_TEST_TAG:-dev}"
bin="${AWM_TEST_BIN_DIR:-$("$repo/scripts/rust-build.sh" --target-dir)/x86_64-unknown-linux-musl/release}"

if [ ! -x "$bin/awm-fake-plugin" ]; then
    "$repo/scripts/rust-build.sh" build --release --target x86_64-unknown-linux-musl --workspace
fi
docker build -t "$reg/awm-test-node:$tag" "$here/images/node"
ctx="$(mktemp -d)"
trap 'rm -rf "$ctx"' EXIT
cp -r "$here/images/stubs/." "$ctx/"
for plugin in "$ctx"/plugins/*/*/; do
    mkdir -p "$plugin/bin"
    cp "$bin/awm-fake-plugin" "$plugin/bin/"
done
docker build --build-arg NODE_IMAGE="$reg/awm-test-node:$tag" -t "$reg/awm-test-stubs:$tag" "$ctx"
if [ "${AWM_TEST_PUSH:-0}" = 1 ]; then
    docker push "$reg/awm-test-node:$tag"
    docker push "$reg/awm-test-stubs:$tag"
fi
