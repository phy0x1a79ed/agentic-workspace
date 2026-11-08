# Sourced by scripts/awm-deploy.sh and scripts/release-*.sh and scripts/refresh-pins.sh. A pin is a key=value file that
# names one built file in <AWM_DIR>/artifacts by version, file and sha256 (deploy.toml
# [artifacts]).

# The id of the page build's inputs: every tracked page source, the shared components and the
# toolchain files. The pages pin is stale exactly when this id moved.
pages_version() {
    echo "pages-$(git -C "$1" ls-files -s -- awm/ui_components 'awm/services/*/pages/*' 'awm/plugins/*/pages/*' 'core/plugins/*/pages/*' \
        awm/scripts/build.sh awm/package.json awm/package-lock.json awm/vite.config.ts awm/svelte.config.js 'awm/tsconfig*.json' \
        | sha256sum | cut -c1-12)"
}

# The id of the core binary's inputs: awm-cli and the local crates it builds from, plus core/'s
# build files (build_id). A crate only a core plugin builds from leaves it alone.
core_version() {
    local paths
    paths="$(core_closure "$1")" || return 1
    echo "core-$(build_id "$1" "$paths" awm-cli)"
}

# The id of the embedder's model files: the sha256 table that names them.
models_version() { echo "models-$(git -C "$1" rev-parse HEAD:core/crates/awm-embed/src/model.rs | cut -c1-12)"; }

core_closure() { crate_closure "$1" core/crates/awm-cli; }

# The core plugins at HEAD: each folder of core/plugins/ that holds a Cargo.toml. The folder
# name is the plugin's name and its binary's; its crate is awm-plugin-<name>.
core_plugins() {
    local name
    git -C "$1" ls-tree HEAD:core/plugins 2>/dev/null | awk -F'\t' '$1 ~ / tree / {print $2}' | while read -r name; do
        git -C "$1" cat-file -e "HEAD:core/plugins/$name/Cargo.toml" 2>/dev/null && printf '%s\n' "$name"
    done
}

# cargo_path_deps <Cargo.toml text on stdin>: one line per local dependency of the build, as
# `path <dir>` or `workspace <name>`, and `define <name> <dir>` per [workspace.dependencies]
# path entry. dev-dependencies are left out: the release binary does not link them.
cargo_path_deps() {
    awk '
        function dep(name, rest,   p) {
            if (match(rest, /path *= *"[^"]*"/)) { p = substr(rest, RSTART, RLENGTH); sub(/^path *= *"/, "", p); sub(/"$/, "", p); print "path " p }
            else if (rest ~ /workspace *= *true/) print "workspace " name
        }
        /^[ \t]*\[/ {
            sec = $0; gsub(/^[ \t]*\[+[ \t]*|[ \t]*\]+.*$/, "", sec); gsub(/[ \t"]/, "", sec)
            table = ""
            if (sec ~ /(^|\.)(dependencies|build-dependencies)\.[^.]+$/) { table = sec; sub(/^.*\./, "", table) }
            if (sec ~ /^workspace\.dependencies\.[^.]+$/) { defn = sec; sub(/^.*\./, "", defn) } else defn = ""
            next
        }
        /^[ \t]*(#|$)/ { next }
        defn != "" { if (match($0, /^[ \t]*path *= *"[^"]*"/)) { p = $0; sub(/^[ \t]*path *= *"/, "", p); sub(/".*$/, "", p); print "define " defn " " p }; next }
        sec == "workspace.dependencies" {
            name = $0; sub(/^[ \t]*/, "", name); sub(/[ \t.=].*$/, "", name)
            if (match($0, /path *= *"[^"]*"/)) { p = substr($0, RSTART, RLENGTH); sub(/^path *= *"/, "", p); sub(/"$/, "", p); print "define " name " " p }
            next
        }
        table != "" { dep(table, $0); next }
        sec ~ /(^|\.)(dependencies|build-dependencies)$/ {
            name = $0; sub(/^[ \t]*/, "", name); sub(/[ \t.=].*$/, "", name); gsub(/"/, "", name)
            rest = $0; sub(/^[^=]*=/, "", rest)
            if ($0 ~ /^[ \t]*[A-Za-z0-9_"-]+\.workspace *= *true/) print "workspace " name
            else dep(name, rest)
        }'
}

# crate_closure <root> <dir>: the repo paths of the crate at <dir> and of every local crate it
# builds from, at HEAD, one per line.
crate_closure() {
    local root="$1" todo seen="" dir deps defs
    defs="$(git -C "$root" show HEAD:core/Cargo.toml | cargo_path_deps | awk '$1 == "define" {print $2, $3}')"
    todo="$2"
    while [ -n "$todo" ]; do
        dir="${todo%%$'\n'*}"
        [ "$todo" = "$dir" ] && todo="" || todo="${todo#*$'\n'}"
        case $'\n'"$seen"$'\n' in *$'\n'"$dir"$'\n'*) continue ;; esac
        seen="${seen:+$seen$'\n'}$dir"
        deps="$(git -C "$root" show "HEAD:$dir/Cargo.toml" | cargo_path_deps | while read -r kind arg; do
            case "$kind" in
                path) resolved="$(realpath -ms "/@repo/$dir/$arg")" ;;
                workspace)
                    resolved="$(printf '%s\n' "$defs" | awk -v n="$arg" '$1 == n {print $2}')"
                    [ -n "$resolved" ] || continue
                    resolved="$(realpath -ms "/@repo/core/$resolved")" ;;
                *) continue ;;
            esac
            case "$resolved" in
                /@repo/*) printf '%s\n' "${resolved#/@repo/}" ;;
                *) echo "error: $dir depends on $arg, outside the repository" >&2; exit 1 ;;
            esac
        done)" || return 1
        [ -z "$deps" ] || todo="${todo:+$todo$'\n'}$deps"
    done
    printf '%s\n' "$seen" | LC_ALL=C sort
}

plugin_closure() { crate_closure "$1" "core/plugins/$2"; }

# The files besides a crate closure that every core binary builds from: the lock, the workspace
# manifest (profile, workspace dependencies) and the Dockerfile (toolchain).
BUILD_FILES="core/Cargo.lock core/Cargo.toml core/Dockerfile"

# locked_deps <root> <package>: one line per package in the lock's closure of <package> at HEAD,
# as `name version checksum`. The lock lists normal, build, proc-macro and dev dependencies alike.
locked_deps() {
    git -C "$1" show HEAD:core/Cargo.lock | awk -v root="$2" '
        function flush() { if (name != "") { key = name " " version; sum[key] = checksum; deps[key] = list; only[name] = (name in only) ? "" : key } name = version = checksum = list = "" }
        function resolve(d,   p) { split(d, p, " "); return (p[2] == "") ? only[p[1]] : p[1] " " p[2] }
        function walk(key,   n, a, i, r) {
            if (key == "" || key in seen) return
            seen[key] = 1
            print key, sum[key]
            n = split(deps[key], a, ",")
            for (i = 1; i <= n; i++) { r = resolve(a[i]); walk(r) }
        }
        /^\[\[package\]\]/ { flush(); next }
        /^name = /     { name = $3; gsub(/"/, "", name); next }
        /^version = /  { version = $3; gsub(/"/, "", version); next }
        /^checksum = / { checksum = $3; gsub(/"/, "", checksum); next }
        /^ "/          { d = $0; sub(/^ "/, "", d); sub(/",?$/, "", d); list = list (list == "" ? "" : ",") d }
        END { flush(); walk(only[root]) }' | LC_ALL=C sort
}

# build_id <root> <closure> <package>: 12 hex of the HEAD object ids of the closure's paths and
# the manifest and Dockerfile, and of the lock's entries for <package>'s dependencies.
build_id() {
    {
        { printf '%s\n' "$2"; printf '%s\n' core/Cargo.toml core/Dockerfile; } \
            | while read -r p; do printf '%s %s\n' "$p" "$(git -C "$1" rev-parse "HEAD:$p")"; done
        locked_deps "$1" "$3"
    } | sha256sum | cut -c1-12
}

# The id of a core plugin binary's inputs: its folder and the local crates it builds from, plus
# core/'s build files (build_id).
plugin_version() {
    local paths
    paths="$(plugin_closure "$1" "$2")" || return 1
    echo "$2-$(build_id "$1" "$paths" "awm-plugin-$2")"
}

plugin_pin() { printf '%s\n' "$1/scripts/plugins/$2.pin"; }
plugin_file() { printf 'awm-plugin-%s-linux-x86_64-musl\n' "$1"; }

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
