"""One-time seed: carry the old ``discord_operators`` allowlist into the
social service's ``social_operators`` table, scoped ``platform='discord'``.

The social service retires the standalone discord service. The discord DB stays
on disk (``$AWM_DIR/services/discord/discord.db``); this reads its
``discord_operators`` rows and upserts them with the new ``platform`` column. A
legacy shared ``state.db`` is also accepted as a fallback source, matching the
old discord seed.

Idempotent (upsert), so re-running is safe. Run against a COPY of any live DB,
never a live file.

Usage:
    python -m awm.social.seed [SOURCE_DB]   # defaults to the discord service DB
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

from awm.social.dao import SocialDAO, init


def _default_source() -> Path:
    """The discord service's own DB under the current workspace."""
    from awm import config
    return config.SERVICES_DIR / "discord" / "discord.db"


def seed_from_discord_db(source_db: str | Path | None = None) -> int:
    """Copy ``discord_operators`` rows into ``social_operators``.

    Returns the number of rows seeded. A missing file or missing table is a
    no-op (returns 0) — a fresh install has nothing to carry over.
    """
    if source_db is None:
        source_db = _default_source()
    source_db = Path(source_db)
    init()
    if not source_db.exists():
        return 0

    src = sqlite3.connect(f"file:{source_db}?mode=ro", uri=True)
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

    dao = SocialDAO()
    n = 0
    with dao.transaction() as conn:
        for r in rows:
            dao.execute(
                """\
                INSERT INTO social_operators
                    (platform, platform_user_id, awm_user, added_at)
                VALUES ('discord', ?, ?, ?)
                ON CONFLICT(platform, platform_user_id) DO UPDATE SET
                    awm_user = excluded.awm_user,
                    added_at = excluded.added_at
                """,
                (r["discord_user_id"], r["awm_user"] or "", r["added_at"] or ""),
                conn=conn,
            )
            n += 1
    return n


def main() -> None:
    source = sys.argv[1] if len(sys.argv) > 1 else None
    n = seed_from_discord_db(source)
    print(f"seeded {n} social_operators row(s) from discord")


if __name__ == "__main__":
    main()
