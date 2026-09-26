#!/bin/bash
# Package game-bot-control as a portal-shaped zip for a desktop client.
#
# The mod is private, so the server cannot push it to a joining client and the
# client must install it by hand. Factorio reads a mod zip only if its top-level
# folder is named <name>_<version>, so the layout matters more than the contents.
# Prints the path of the zip it wrote.
set -euo pipefail

cd "$(dirname "$0")"
SRC=mods/game-bot-control
OUT="${1:-dist}"
mkdir -p "$OUT"

python3 - "$SRC" "$OUT" <<'PY'
import json, sys, zipfile
from pathlib import Path

src, out = Path(sys.argv[1]), Path(sys.argv[2])
info = json.loads((src / "info.json").read_text())
folder = f"{info['name']}_{info['version']}"
target = out / f"{folder}.zip"
with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as z:
    for f in sorted(src.iterdir()):
        if f.is_file():
            z.write(f, f"{folder}/{f.name}")
print(target.resolve())
PY
