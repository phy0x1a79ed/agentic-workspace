#!/usr/bin/env bash
# Build the static musl core binary in mizar's Rust container and pin it in scripts/core.pin.
#
# The binary goes into <AWM_DIR>/artifacts under a name that carries its version, which is the
# git tree id of core/. The pin is stale exactly when core/ changed since this ran. Commit the
# pin; awm's deploy then pushes the binary to each push node (deploy.toml [artifacts]) and
# scripts/awm-deploy.sh refuses a pin that does not match core/.
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
. "$root/scripts/pins.sh"
store="${AWM_DIR:-$root/.awm}/artifacts"
target=x86_64-unknown-linux-musl

[ -z "$(git -C "$root" status --porcelain -- core)" ] || {
    echo "core/ has uncommitted changes: commit them, so the pin names a tree that exists" >&2; exit 1; }
version="$(core_version "$root")"
file="awm-$version-linux-x86_64-musl"

"$root/scripts/rust-build.sh" build --release --target "$target" -p awm-cli
store_artifact "$root/core/target/$target/release/awm" "$file" "$store"
write_pin "$root/scripts/core.pin" "The awm core binary (static musl) that a deploy places at ~/.local/bin/awm." \
    "$version" "$file" "$store"
echo "pinned $file in scripts/core.pin"
