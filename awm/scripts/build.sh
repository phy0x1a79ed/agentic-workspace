#!/usr/bin/env bash
# Build every page to its own dist/ using the single root Vite config.
#
# A "page" is any pages/<name>/ dir that has an index.html — the frontend
# analogue of the Python service marker (a dir with an `awm/` package). Source-
# only placeholder dirs (no index.html yet) are skipped cleanly. Each page is
# built with cwd = the page dir, so outDir:'dist' and $lib resolve page-locally;
# output lands in <page>/dist/, the same path the serve layer reads.
#
# AWM_PAGE_ROOTS (a ':' list, from .awm/env under `awm deploy`) adds page roots
# outside this tree. They build with this tree's toolchain; a name already
# taken by an earlier root is skipped, as in gateway discovery.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VITE="$ROOT/node_modules/.bin/vite"

if [ ! -x "$VITE" ]; then
  echo "vite not found at $VITE — run 'npm install' in $ROOT first" >&2
  exit 1
fi

declare -A seen=()
built=0
IFS=: read -ra extra <<<"${AWM_PAGE_ROOTS:-}"
for root in "$ROOT/pages" "${extra[@]}"; do
  [ -n "$root" ] || continue
  if [ ! -d "$root" ]; then
    echo "skipping page root ${root}: not a directory" >&2
    continue
  fi
  for d in "$root"/*/; do
    [ -d "$d" ] || continue
    name="$(basename "$d")"
    case "$name" in .*|_*) continue ;; esac
    if [ -n "${seen[$name]:-}" ]; then
      echo "skipping ${d}: shadowed by ${seen[$name]}" >&2
      continue
    fi
    seen[$name]="$d"
    [ -f "${d}index.html" ] || continue
    echo "── building ${name}"
    ( cd "$d" && "$VITE" build --config "$ROOT/vite.config.ts" )
    built=$((built + 1))
  done
done
echo "built ${built} page(s)"
