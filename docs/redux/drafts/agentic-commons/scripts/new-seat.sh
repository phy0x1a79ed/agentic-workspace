#!/usr/bin/env bash
# Make seats/<person>/ a local git repository with DVC and no remote (users.md R128 item 4).
set -euo pipefail
person="${1:?usage: new-seat.sh <person-slug>}"
[[ "$person" =~ ^[a-z0-9][a-z0-9-]*$ ]] || { echo "slug must be lowercase letters, digits and hyphens" >&2; exit 2; }
root="$(cd "$(dirname "$0")/.." && pwd)"
seat="$root/seats/$person"
if [[ -d "$seat/.git" ]]; then echo "$seat"; exit 0; fi
mkdir -p "$seat/data"
git -C "$seat" init -q -b main
printf '/data/*\n!/data/*.dvc\n!/data/.gitignore\n' > "$seat/.gitignore"
if command -v dvc >/dev/null; then (cd "$seat" && dvc init -q); fi
printf '# %s\n\nThis seat folder belongs to %s. Only their seat writes here.\n' "$person" "$person" > "$seat/README.md"
git -C "$seat" add -A
git -C "$seat" -c user.name=Opal -c user.email=opal@localhost commit -q -m "seat folder for $person"
echo "$seat"
