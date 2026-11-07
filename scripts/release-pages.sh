#!/usr/bin/env bash
# Build every plugin page on mizar and pin the result in scripts/pages.pin.
#
# The artifact is a tarball of each plugin's pages/<name>/dist folder, with paths relative to
# the checkout root. A push node unpacks it and never builds (scripts/awm-deploy.sh), so only
# this node needs node and awm/node_modules. The pin is stale exactly when a page source, a
# shared component or the toolchain files changed since this ran. scripts/refresh-pins.sh runs
# this for awm's deploy and commits the pin.
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
. "$root/scripts/pins.sh"
store="${AWM_DIR:-$root/.awm}/artifacts"

[ -z "$(git -C "$root" status --porcelain -- awm 'core/plugins/*/pages')" ] || {
    echo "awm/ or a core plugin's pages have uncommitted changes: commit them, so the pin names a tree that exists" >&2; exit 1; }
version="$(pages_version "$root")"
file="awm-$version.tar.gz"

bash "$root/awm/scripts/build.sh"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
roots=(awm/services awm/plugins)
[ ! -d "$root/core/plugins" ] || roots+=(core/plugins)
(cd "$root" && find "${roots[@]}" -mindepth 4 -maxdepth 4 -type d -name dist -path '*/pages/*/dist' \
    | LC_ALL=C sort > "$tmp/dists")
[ -s "$tmp/dists" ] || { echo "the build left no pages/*/dist folder" >&2; exit 1; }
tar -C "$root" --sort=name --mtime=@0 --owner=0 --group=0 --numeric-owner -czf "$tmp/pages.tar.gz" -T "$tmp/dists"
store_artifact "$tmp/pages.tar.gz" "$file" "$store"
write_pin "$root/scripts/pages.pin" "The built plugin pages that a deploy unpacks on every node, so no node but mizar builds." \
    "$version" "$file" "$store"
echo "pinned $file ($(wc -l < "$tmp/dists") page folders) in scripts/pages.pin"
