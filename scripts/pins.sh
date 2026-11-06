# Sourced by scripts/awm-deploy.sh and scripts/release-*.sh and scripts/refresh-pins.sh. A pin is a key=value file that
# names one built file in <AWM_DIR>/artifacts by version, file and sha256 (deploy.toml
# [artifacts]).

# The id of the page build's inputs: every tracked page source, the shared components and the
# toolchain files. The pages pin is stale exactly when this id moved.
pages_version() {
    echo "pages-$(git -C "$1" ls-files -s -- awm/ui_components 'awm/services/*/pages/*' 'awm/plugins/*/pages/*' \
        awm/scripts/build.sh awm/package.json awm/package-lock.json awm/vite.config.ts awm/svelte.config.js 'awm/tsconfig*.json' \
        | sha256sum | cut -c1-12)"
}

# The id of the core binary's inputs: the git tree of core/.
core_version() { echo "core-$(git -C "$1" rev-parse --short=12 HEAD:core)"; }

pin_field() { sed -n "s/^$2=//p" "$1" | head -1; }

# verify_pin <pin file> <wanted version> <artifact store>: print the verified file's path, or
# say why the pin cannot be used and return 1.
verify_pin() {
    local pin="$1" want="$2" store="$3" name version file sha got
    name="$(basename "$pin")"
    version="$(pin_field "$pin" version)"; file="$(pin_field "$pin" file)"; sha="$(pin_field "$pin" sha256)"
    if [ -z "$version" ] || [ -z "$file" ] || [ -z "$sha" ]; then
        echo "error: $name needs version, file and sha256." >&2; return 1
    fi
    if [ "$version" != "$want" ]; then
        echo "error: $name names $version, and the tree is at $want. awm's deploy on mizar rebuilds a stale pin (scripts/refresh-pins.sh), so this release did not go through it." >&2
        return 1
    fi
    if [ ! -f "$store/$file" ]; then
        echo "error: no $file in $store. awm's deploy on mizar pushes it to this node." >&2; return 1
    fi
    got="$(sha256sum "$store/$file" | cut -d' ' -f1)"
    if [ "$got" != "$sha" ]; then
        echo "error: $store/$file has sha256 $got, and $name names $sha." >&2; return 1
    fi
    printf '%s\n' "$store/$file"
}

# store_artifact <built file> <file name> <artifact store>: place it atomically in the store.
store_artifact() {
    local tmp
    mkdir -p "$3"
    tmp="$(mktemp "$3/.$2.XXXXXX")"
    cp "$1" "$tmp"
    chmod 0755 "$tmp"
    mv "$tmp" "$3/$2"
}

# write_pin <pin file> <description> <version> <file> <store>
write_pin() {
    printf '# %s\n# Written by the deploy on mizar (scripts/refresh-pins.sh). Do not edit.\nversion=%s\nfile=%s\nsha256=%s\n' \
        "$2" "$3" "$4" "$(sha256sum "$5/$4" | cut -d' ' -f1)" > "$1"
}
