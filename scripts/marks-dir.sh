#!/usr/bin/env bash
# Put this node's private marks in <AWM_DIR>/marks/, which a plugin unit can bind read-only,
# and leave <AWM_DIR>/private.json as a relative symlink to the file there. Idempotent.
#   marks-dir.sh [<AWM_DIR>]
#
# Readers take marks/private.json once marks/ exists, so the file is in place before the link
# swaps in, and the old path never goes missing: a reader that found no file there would read
# "no marks". Both names stay one file until the link swaps in. The group is awm-marks when that
# group exists.
set -euo pipefail
awm_dir="${1:-${AWM_DIR:?set AWM_DIR or pass it}}"
dir="$awm_dir/marks"
legacy="$awm_dir/private.json"
file="$dir/private.json"

mkdir -p "$dir"
chmod 0750 "$dir"
group=""
getent group awm-marks >/dev/null 2>&1 && group=awm-marks
[ -z "$group" ] || chgrp "$group" "$dir"

# A writer still running old code replaces the legacy file in place, so the link swaps in only
# while both names are still one file.
for _ in 1 2 3 4 5; do
    if [ -f "$legacy" ] && [ ! -L "$legacy" ]; then
        if [ -e "$file" ] && ! [ "$legacy" -ef "$file" ]; then
            ln -f "$legacy" "$dir/.private.json.new"
            mv -T "$dir/.private.json.new" "$file"
        fi
        [ -e "$file" ] || ln "$legacy" "$file"
    fi
    [ -f "$file" ] || exit 0
    chmod 0640 "$file"
    [ -z "$group" ] || chgrp "$group" "$file"
    [ "$(readlink "$legacy" 2>/dev/null)" != "marks/private.json" ] || exit 0
    ln -sfn marks/private.json "$awm_dir/.private.json.link"
    if [ ! -e "$legacy" ] || [ -L "$legacy" ] || [ "$legacy" -ef "$file" ]; then
        mv -T "$awm_dir/.private.json.link" "$legacy"
        exit 0
    fi
    rm -f "$awm_dir/.private.json.link"
done
echo "error: $legacy kept changing; run $0 again" >&2
exit 1
