#!/usr/bin/env bash
# Build the static musl core binary in mizar's Rust container and pin it in scripts/core.pin.
#
# The binary goes into <AWM_DIR>/artifacts under a name that carries its version, the id of
# awm-cli's crate closure and core/'s build files (scripts/pins.sh core_version). The pin is
# stale exactly when one of those changed since this ran; a core plugin has its own pin
# (release-plugins.sh).
# scripts/refresh-pins.sh runs this for awm's deploy and commits the pin; the deploy then pushes
# the binary to each push node (deploy.toml [artifacts]) and scripts/awm-deploy.sh refuses a pin
# that does not match core/. AWM_CARGO_TARGET_DIR names a persistent cargo target dir.
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
. "$root/scripts/pins.sh"
store="${AWM_DIR:-$root/.awm}/artifacts"
target=x86_64-unknown-linux-musl
target_dir="${AWM_CARGO_TARGET_DIR:-$root/core/target}"

dirty="$(git -C "$root" status --porcelain -- $(core_closure "$root") $BUILD_FILES)"
[ -z "$dirty" ] || {
    echo "the core builds from uncommitted changes: commit them, so the pin names a tree that exists" >&2
    printf '%s\n' "$dirty" >&2; exit 1; }
version="$(core_version "$root")"
file="awm-$version-linux-x86_64-musl"

"$root/scripts/rust-build.sh" build --locked --release --target "$target" -p awm-cli
store_artifact "$target_dir/$target/release/awm" "$file" "$store"
write_pin "$root/scripts/core.pin" "The awm core binary (static musl) that a deploy places at ~/.local/bin/awm." \
    "$version" "$file" "$store"
echo "pinned $file in scripts/core.pin"
