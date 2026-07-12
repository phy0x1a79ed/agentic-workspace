#!/usr/bin/env bash
# Build every page to its own dist/ using the single root Vite config.
#
# A "page" is any pages/<name>/ dir that has an index.html — the frontend
# analogue of the Python service marker (a dir with an `awm/` package). Source-
# only placeholder dirs (no index.html yet) are skipped cleanly. Each page is
# built with cwd = the page dir, so outDir:'dist' and $lib resolve page-locally;
# output lands in awm/pages/<name>/dist/, the same path the serve layer reads.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VITE="$ROOT/node_modules/.bin/vite"

if [ ! -x "$VITE" ]; then
  echo "vite not found at $VITE — run 'npm install' in $ROOT first" >&2
  exit 1
fi

built=0
for d in "$ROOT"/pages/*/; do
  [ -f "${d}index.html" ] || continue
  name="$(basename "$d")"
  echo "── building ${name}"
  ( cd "$d" && "$VITE" build --config "$ROOT/vite.config.ts" )
  built=$((built + 1))
done
echo "built ${built} page(s)"
