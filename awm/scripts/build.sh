#!/usr/bin/env bash
# Build every plugin page to its own dist/ using the single root Vite config.
#
# A page is a plugin's pages/<name>/ dir with an index.html; the plugin's
# awm-plugin.toml names it in a [[page]] with dir = "pages/<name>/dist". A
# plugin is a folder with an awm-plugin.toml under services/, plugins/ or the
# checkout's core/plugins/. Each
# page is built with cwd = the page dir, so outDir:'dist' and $lib resolve
# page-locally.
#
# Only mizar runs this, through scripts/release-pages.sh: a push node unpacks the pinned
# result and needs no node.
#
# AWM_PLUGIN_ROOTS (a ':' list) adds plugin roots outside this tree, laid out
# like services/. Their pages build with this tree's toolchain.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VITE="$ROOT/node_modules/.bin/vite"

node_major="$(node --version 2>/dev/null | sed -E 's/^v([0-9]+).*/\1/')"
[ "${node_major:-0}" -ge 18 ] 2>/dev/null && command -v npm >/dev/null || {
  echo "the page build needs nodejs >= 18 and npm on PATH (found: $(node --version 2>/dev/null || echo no node))" >&2; exit 1; }

# node_modules follows package-lock.json: a lock that changed since the last install gets `npm ci`.
lock_sha="$(sha256sum "$ROOT/package-lock.json" | cut -d' ' -f1)"
if [ ! -x "$VITE" ] || [ "$(cat "$ROOT/node_modules/.lock-sha256" 2>/dev/null || true)" != "$lock_sha" ]; then
  echo "── npm ci (package-lock.json changed)"
  ( cd "$ROOT" && npm ci )
  printf '%s\n' "$lock_sha" > "$ROOT/node_modules/.lock-sha256"
fi

built=0
IFS=: read -ra extra <<<"${AWM_PLUGIN_ROOTS:-}"
core_plugins="$(dirname "$ROOT")/core/plugins"
for root in "$core_plugins" "$ROOT/services" "$ROOT/plugins" "${extra[@]}"; do
  [ -n "$root" ] || continue
  # A tree with no Rust plugin yet has no core/plugins.
  [ "$root" != "$core_plugins" ] || [ -d "$root" ] || continue
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
