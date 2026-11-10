#!/usr/bin/env bash
# Pack the embedder's model files from this node's <AWM_DIR>/models into the artifact store and
# write scripts/models.pin. MINILM's sha256 table in core/crates/awm-embed/src/model.rs names the
# files. Only MiniLM deploys to every node: bge-large travels inside the papers generation.
# A new revision goes into <AWM_DIR>/models on the landing node first, so this never downloads.
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
. "$root/scripts/pins.sh"
awm_dir="${AWM_DIR:-$root/.awm}"
store="$awm_dir/artifacts"
table="$root/core/crates/awm-embed/src/model.rs"

minilm="$(sed -n '/^pub const MINILM: Model/,/^};/p' "$table")"
field() { sed -n "s/^ *$1: \"\(.*\)\",$/\1/p" <<<"$minilm"; }
model="$(field name)" revision="$(field revision)"
rel="$model/$revision"
files=()
while read -r name sha; do
    got="$(sha256sum "$awm_dir/models/$rel/$name" | cut -d' ' -f1)"
    [ "$got" = "$sha" ] || { echo "error: $awm_dir/models/$rel/$name has sha256 $got, and model.rs names $sha." >&2; exit 1; }
    files+=("$rel/$name")
done < <(sed -n 's/^ *("\([^"]*\)", "\([0-9a-f]\{64\}\)"),$/\1 \2/p' <<<"$minilm")
[ "${#files[@]}" -gt 0 ] || { echo "error: no MINILM files in the table in $table" >&2; exit 1; }

version="$(models_version "$root")"
file="awm-models-${version#models-}.tar"
built="$(mktemp -d)"
trap 'rm -rf "$built"' EXIT
tar --sort=name --mtime=@0 --owner=0 --group=0 --numeric-owner -cf "$built/$file" -C "$awm_dir/models" "${files[@]}"
store_artifact "$built/$file" "$file" "$store"
write_pin "$root/scripts/models.pin" "The embedder's model files, which scripts/awm-deploy.sh unpacks into <AWM_DIR>/models." "$version" "$file" "$store"
echo "models: $file"
