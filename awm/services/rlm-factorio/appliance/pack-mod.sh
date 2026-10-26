#!/bin/bash
# Package mods as portal-shaped zips for a desktop client.
#
# Usage: pack-mod.sh [OUT [MOD_DIR...]]. OUT defaults to dist, and the mods to
# game-bot-control. A lab also needs the lab mods (RLM_FACTORIO_LAB_MODS).
#
# The mods are private, so the server cannot push them to a joining client and
# the client must install them by hand. Factorio reads a mod zip only if its
# top-level folder is named <name>_<version>, so the layout matters more than the
# contents. Prints the path of each zip it wrote.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
OUT="$(realpath -m "${1:-$HERE/dist}")"
shift || true
[ $# -gt 0 ] || set -- "$HERE/mods/game-bot-control"
mkdir -p "$OUT"

python3 - "$OUT" "$@" <<'PY'
import json, sys, zipfile
from pathlib import Path

out = Path(sys.argv[1])
for src in map(Path, sys.argv[2:]):
    info = json.loads((src / "info.json").read_text())
    folder = f"{info['name']}_{info['version']}"
    target = out / f"{folder}.zip"
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(src.rglob("*")):
            if f.is_file():
                z.write(f, f"{folder}/{f.relative_to(src)}")
    print(target.resolve())
PY
