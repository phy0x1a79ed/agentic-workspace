#!/usr/bin/env bash
# The node-local deploy. It runs the core binary that scripts/core.pin names and unpacks the
# page dists that scripts/pages.pin names, both from <AWM_DIR>/artifacts, where mizar's deploy
# pushed them (scripts/refresh-pins.sh built them). This script builds
# nothing. A sha256 that differs from a pin, or a pin older than the tree, stops the deploy.
# Arguments pass through to `awm deploy`.
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
. "$root/scripts/pins.sh"
store="${AWM_DIR:-$root/.awm}/artifacts"

bin="$(verify_pin "$root/scripts/core.pin" "$(core_version "$root")" "$store")"
pages="$(verify_pin "$root/scripts/pages.pin" "$(pages_version "$root")" "$store")"

# Replace each page folder the tarball holds, so a page that lost a file loses it here too.
tar -tzf "$pages" | sed -n 's|^\(.*/dist\)/$|\1|p' | while read -r dist; do rm -rf "${root:?}/$dist"; done
tar -xzf "$pages" -C "$root"

chmod 0755 "$bin"
exec "$bin" deploy --root "$root" --bin "$bin" --no-build "$@"
