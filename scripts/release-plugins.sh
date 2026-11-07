#!/usr/bin/env bash
# Build the static musl binary of each core plugin whose pin is stale, in mizar's Rust
# container, and pin each in scripts/plugins/<name>.pin.
#
# A core plugin is a folder core/plugins/<name>/ with a cargo crate awm-plugin-<name> whose
# binary is <name>. Its version covers its folder, the local crates it builds from and core/'s
# Cargo.lock, Cargo.toml and Dockerfile (scripts/pins.sh plugin_version), so a change to one
# plugin rebuilds only that plugin. scripts/refresh-pins.sh runs this for awm's deploy and
# commits the pins; deploy.toml [artifacts] pins lists each pin, so the deploy pushes its binary
# to each push node, and scripts/awm-deploy.sh places it. AWM_CARGO_TARGET_DIR names a
# persistent cargo target dir.
#   release-plugins.sh [--dry-run]    --dry-run prints each plugin's version and pin state
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
. "$root/scripts/pins.sh"
store="${AWM_DIR:-$root/.awm}/artifacts"
target=x86_64-unknown-linux-musl
target_dir="${AWM_CARGO_TARGET_DIR:-$root/core/target}"
dry=""
[ "${1:-}" != "--dry-run" ] || dry=1

stale=() versions=()
for name in $(core_plugins "$root"); do
    version="$(plugin_version "$root" "$name")"
    pin="$(plugin_pin "$root" "$name")"
    grep -q "\"scripts/plugins/$name.pin\"" "$root/deploy.toml" || {
        echo "error: deploy.toml [artifacts] pins does not list scripts/plugins/$name.pin, so no push node would get $name" >&2
        exit 1; }
    if [ -f "$pin" ] && verify_pin "$pin" "$version" "$store" >/dev/null 2>&1; then
        echo "$name: $version is pinned"
        continue
    fi
    echo "$name: $version is stale"
    stale+=("$name"); versions+=("$version")
done
for pin in "$root"/scripts/plugins/*.pin; do
    [ -e "$pin" ] || continue
    name="$(basename "$pin" .pin)"
    [ -d "$root/core/plugins/$name" ] || echo "warning: $pin pins $name, which core/plugins does not hold" >&2
done
[ "${#stale[@]}" -gt 0 ] || exit 0
[ -z "$dry" ] || { echo "--- dry run: nothing built ---"; exit 0; }

packages=()
for i in "${!stale[@]}"; do
    name="${stale[$i]}"
    # cargo builds the working tree: a change not committed would ship under the wrong version.
    dirty="$(git -C "$root" status --porcelain -- $(plugin_closure "$root" "$name") $BUILD_FILES)"
    [ -z "$dirty" ] || {
        echo "$name builds from uncommitted changes: commit them, so the pin names a tree that exists" >&2
        printf '%s\n' "$dirty" >&2; exit 1; }
    packages+=(-p "awm-plugin-$name")
done

"$root/scripts/rust-build.sh" build --locked --release --target "$target" "${packages[@]}"
mkdir -p "$root/scripts/plugins"
for i in "${!stale[@]}"; do
    name="${stale[$i]}" version="${versions[$i]}"
    file="$(plugin_file "$version")"
    store_artifact "$target_dir/$target/release/$name" "$file" "$store"
    write_pin "$(plugin_pin "$root" "$name")" \
        "The $name core plugin binary (static musl) that a deploy places at <AWM_DIR>/bin/$name." \
        "$version" "$file" "$store"
    echo "pinned $file in scripts/plugins/$name.pin"
done
