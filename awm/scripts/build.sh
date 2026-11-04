#!/usr/bin/env bash
# Build every plugin page to its own dist/ using the single root Vite config.
#
# A page is a plugin's pages/<name>/ dir with an index.html; the plugin's
# awm-plugin.toml names it in a [[page]] with dir = "pages/<name>/dist". A
# plugin is a folder with an awm-plugin.toml under services/ or plugins/. Each
# page is built with cwd = the page dir, so outDir:'dist' and $lib resolve
# page-locally.
#
# AWM_PLUGIN_ROOTS (a ':' list) adds plugin roots outside this tree, laid out
# like services/. Their pages build with this tree's toolchain.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VITE="$ROOT/node_modules/.bin/vite"

if [ ! -x "$VITE" ]; then
  echo "vite not found at $VITE — run 'npm install' in $ROOT first" >&2
  exit 1
fi

built=0
IFS=: read -ra extra <<<"${AWM_PLUGIN_ROOTS:-}"
for root in "$ROOT/services" "$ROOT/plugins" "${extra[@]}"; do
  [ -n "$root" ] || continue
  if [ ! -d "$root" ]; then
    echo "skipping plugin root ${root}: not a directory" >&2
    continue
  fi
  for plugin in "$root"/*/; do
    [ -f "${plugin}awm-plugin.toml" ] || continue
    for d in "$plugin"pages/*/; do
      [ -f "${d}index.html" ] || continue
      echo "── building $(basename "$plugin")/$(basename "$d")"
      ( cd "$d" && "$VITE" build --config "$ROOT/vite.config.ts" )
      built=$((built + 1))
    done
  done
done
echo "built ${built} page(s)"
