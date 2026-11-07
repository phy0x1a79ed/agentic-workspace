#!/usr/bin/env bash
# The node-local deploy. It runs the core binary that scripts/core.pin names, places each core
# plugin binary that scripts/plugins/<name>.pin names at <AWM_DIR>/bin/<name>, and unpacks the
# page dists that scripts/pages.pin names, all from <AWM_DIR>/artifacts, where mizar's deploy
# pushed them (scripts/refresh-pins.sh built them). This script builds nothing. A sha256 that
# differs from a pin, or a pin older than the tree, stops the deploy.
#
# A plugin binary is replaced only when its bytes differ from the pin's, and the reload then
# restarts that plugin: the gateway restarts only the plugins a deploy names, and a running
# service outlives a gateway restart. Arguments pass through to `awm deploy`; --dry-run also
# keeps the plugin binaries in place.
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
. "$root/scripts/pins.sh"
awm_dir="${AWM_DIR:-$root/.awm}"
store="$awm_dir/artifacts"

dry=""
for arg in "$@"; do [ "$arg" != "--dry-run" ] || dry=1; done

bin="$(verify_pin "$root/scripts/core.pin" "$(core_version "$root")" "$store")"
pages="$(verify_pin "$root/scripts/pages.pin" "$(pages_version "$root")" "$store")"
plugins=() sources=()
for name in $(core_plugins "$root"); do
    pin="$(plugin_pin "$root" "$name")"
    [ -f "$pin" ] || { echo "error: core plugin $name has no scripts/plugins/$name.pin. awm's deploy on mizar writes it (scripts/refresh-pins.sh), so this release did not go through it." >&2; exit 1; }
    plugins+=("$name")
    sources+=("$(verify_pin "$pin" "$(plugin_version "$root" "$name")" "$store")")
done

# Replace each page folder the tarball holds, so a page that lost a file loses it here too.
tar -tzf "$pages" | sed -n 's|^\(.*/dist\)/$|\1|p' | while read -r dist; do rm -rf "${root:?}/$dist"; done
tar -xzf "$pages" -C "$root"

# A plugin restarts when this deploy replaced its binary, or when an earlier deploy did and
# failed before its reload (the marker <AWM_DIR>/bin/.<name>.restart outlives that deploy).
restart=()
for i in "${!plugins[@]}"; do
    name="${plugins[$i]}" src="${sources[$i]}" dest="$awm_dir/bin/${plugins[$i]}"
    if [ -f "$dest" ] && [ "$(sha256sum < "$dest" | cut -d' ' -f1)" = "$(pin_field "$(plugin_pin "$root" "$name")" sha256)" ]; then
        if [ -e "$awm_dir/bin/.$name.restart" ]; then
            echo "plugin $name: $dest is current, and its restart is pending"
            restart+=("$name")
        fi
        continue
    fi
    restart+=("$name")
    if [ -n "$dry" ]; then
        echo "plugin $name: would place $src at $dest"
        continue
    fi
    echo "plugin $name: place $src at $dest"
    mkdir -p "$awm_dir/bin"
    : > "$awm_dir/bin/.$name.restart"
    store_artifact "$src" "$name" "$awm_dir/bin"
done
args=(deploy --root "$root" --bin "$bin" --no-build)
[ "${#restart[@]}" -eq 0 ] || args+=(--reload-restart "$(IFS=,; echo "${restart[*]}")")

[ -x "$bin" ] || chmod 0755 "$bin"
if [ "${#restart[@]}" -eq 0 ] || [ -n "$dry" ]; then
    exec "$bin" "${args[@]}" "$@"
fi
"$bin" "${args[@]}" "$@"
for name in "${restart[@]}"; do rm -f "$awm_dir/bin/.$name.restart"; done
