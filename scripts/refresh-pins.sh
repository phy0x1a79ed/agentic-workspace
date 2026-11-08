#!/usr/bin/env bash
# Rebuild each stale pin of this tree: the core binary (scripts/release-core.sh), each core
# plugin binary (scripts/release-plugins.sh), the plugin pages (scripts/release-pages.sh) and
# the embedder's model files (scripts/release-models.sh).
# A pin is stale when its version differs from the tree's, or when its file is missing from, or
# differs in, the artifact store. awm's deploy runs this in the batch's temp worktree
# (deploy.toml [artifacts] refresh) and commits the pins it rewrote.
#
# The service runs this with the gateway's environment, so it sets the toolchains itself:
# npm from the dsh conda env, cargo through the Rust container. The caches survive the temp
# tree: the cargo target dir, and node_modules keyed by package-lock.json.
#   AWM_DIR            the landing node's awm dir; the artifact store is <AWM_DIR>/artifacts
#   AWM_BUILD_CACHE    the cache root, default <AWM_DIR>/build-cache
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
. "$root/scripts/pins.sh"
export AWM_DIR="${AWM_DIR:-$root/.awm}"
store="$AWM_DIR/artifacts"
cache="${AWM_BUILD_CACHE:-$AWM_DIR/build-cache}"
conda="${AWM_CONDA_ROOT:-$HOME/miniforge3}"
export PATH="$conda/envs/dsh/bin:$PATH"

if ! verify_pin "$root/scripts/models.pin" "$(models_version "$root")" "$store" >/dev/null 2>&1; then
    echo "── models pin is stale"
    "$root/scripts/release-models.sh"
fi

if ! verify_pin "$root/scripts/core.pin" "$(core_version "$root")" "$store" >/dev/null 2>&1; then
    echo "── core pin is stale"
    AWM_CARGO_TARGET_DIR="$cache/cargo-target" "$root/scripts/release-core.sh"
fi

for name in $(core_plugins "$root"); do
    if ! verify_pin "$(plugin_pin "$root" "$name")" "$(plugin_version "$root" "$name")" "$store" >/dev/null 2>&1; then
        echo "── core plugin pins are stale"
        AWM_CARGO_TARGET_DIR="$cache/cargo-target" "$root/scripts/release-plugins.sh"
        break
    fi
done

if ! verify_pin "$root/scripts/pages.pin" "$(pages_version "$root")" "$store" >/dev/null 2>&1; then
    echo "── pages pin is stale"
    modules="$root/awm/node_modules"
    lock="$(sha256sum "$root/awm/package-lock.json" | cut -c1-16)"
    kept="$cache/node_modules/$lock"
    if [ -d "$kept" ] && [ ! -e "$modules" ]; then
        echo "── node_modules from the cache"
        cp -a --reflink=auto "$kept" "$modules"
    fi
    "$root/scripts/release-pages.sh"
    if [ ! -d "$kept" ] && [ -d "$modules" ]; then
        mkdir -p "$cache/node_modules"
        tmp="$(mktemp -d "$cache/node_modules/.new.XXXXXX")"
        cp -a --reflink=auto "$modules/." "$tmp"
        mv -T "$tmp" "$kept"
        find "$cache/node_modules" -mindepth 1 -maxdepth 1 ! -name "$lock" -exec rm -rf {} +
    fi
fi
