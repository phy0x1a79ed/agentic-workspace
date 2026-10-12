"""Copy the door's database to the reporting service's and bring it to the current schema.

    python -m awm.reporting.migrate [--src PATH] [--dst PATH]

Run it once, with the service stopped. It refuses to overwrite a destination.
"""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

from awm.reporting import config
from awm.reporting.store import Db


def migrate(src: Path, dst: Path) -> None:
    if dst.exists():
        raise FileExistsError(f"{dst} exists; the migration never overwrites")
    if not src.is_file():
        raise FileNotFoundError(src)
    dst.parent.mkdir(parents=True, exist_ok=True)
    source, target = sqlite3.connect(src), sqlite3.connect(dst)
    try:
        source.backup(target)
    finally:
        source.close()
        target.close()
    Db(dst)


def main() -> None:
    dst = config.db_path()
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--src", type=Path, default=dst.parent.parent / "door" / "door.db")
    ap.add_argument("--dst", type=Path, default=dst)
    args = ap.parse_args()
    migrate(args.src, args.dst)
    print(f"migrated {args.src} -> {args.dst}")


if __name__ == "__main__":
    main()
