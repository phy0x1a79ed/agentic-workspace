"""One-time seed: carry ``discord_operators`` from the legacy shared
``state.db`` into the discord service's own DB.

The modular cutover drops the shared runtime DB but leaves the old file on
disk as a read-only legacy source. Each service extracts its own rows. For
discord this is a straight copy — ``awm_user`` was already a plain username
literal in legacy, so there is no ``agent_id`` → natural-key re-keying to do.

Idempotent (upsert), so re-running is safe. Run against a COPY of any live
``state.db``, never the live file.

Usage:
    python -m awm.discord.seed [LEGACY_STATE_DB]   # defaults to awm.config.DB_PATH
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

from awm.discord.dao import DiscordDAO, init


def seed_from_legacy(legacy_db: str | Path | None = None) -> int:
    """Copy legacy ``discord_operators`` rows into the discord DB.

    Returns the number of rows seeded. A missing legacy file or missing table
    is a no-op (returns 0) — a fresh install has nothing to carry over.
    """
    if legacy_db is None:
        from awm.config import DB_PATH
        legacy_db = DB_PATH
    legacy_db = Path(legacy_db)
    init()
    if not legacy_db.exists():
        return 0

    src = sqlite3.connect(f"file:{legacy_db}?mode=ro", uri=True)
    src.row_factory = sqlite3.Row
    try:
        tbl = src.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name='discord_operators'"
        ).fetchone()
        if tbl is None:
            return 0
        rows = src.execute(
            "SELECT discord_user_id, awm_user, added_at FROM discord_operators"
        ).fetchall()
    finally:
        src.close()

    dao = DiscordDAO()
    n = 0
    with dao.transaction() as conn:
        for r in rows:
            dao.execute(
                """\
                INSERT INTO discord_operators (discord_user_id, awm_user, added_at)
                VALUES (?, ?, ?)
                ON CONFLICT(discord_user_id) DO UPDATE SET
                    awm_user = excluded.awm_user,
                    added_at = excluded.added_at
                """,
                (r["discord_user_id"], r["awm_user"] or "", r["added_at"] or ""),
                conn=conn,
            )
            n += 1
    return n


def main() -> None:
    legacy = sys.argv[1] if len(sys.argv) > 1 else None
    n = seed_from_legacy(legacy)
    print(f"seeded {n} discord_operators row(s)")


if __name__ == "__main__":
    main()
