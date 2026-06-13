"""One-time seed: carry ``embeddings WHERE source_type='skill'`` from the
legacy shared ``state.db`` into the skills service's own DB.

The modular cutover drops the shared runtime DB but leaves the old file on
disk as a read-only legacy source. Each service extracts its own rows. For
skills this is a straight copy — the ``embeddings`` table shape is identical
in legacy and v1 (same DDL), and skill rows carry no FK that needs re-keying.

Idempotent (upsert), so re-running is safe. Run against a COPY of any live
``state.db``, never the live file.

Usage:
    python -m awm.skills.seed [LEGACY_STATE_DB]   # defaults to awm.config.DB_PATH
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

from awm.skills.dao import SkillsDAO, init


def seed_from_legacy(legacy_db: str | Path | None = None) -> int:
    """Copy legacy ``embeddings WHERE source_type='skill'`` into the skills DB.

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
            "WHERE type='table' AND name='embeddings'"
        ).fetchone()
        if tbl is None:
            return 0
        rows = src.execute(
            "SELECT source_type, source_id, chunk_text, embedding, updated_at "
            "FROM embeddings WHERE source_type = 'skill'"
        ).fetchall()
    finally:
        src.close()

    dao = SkillsDAO()
    n = 0
    with dao.transaction() as conn:
        for r in rows:
            dao.execute(
                """\
                INSERT INTO embeddings
                    (source_type, source_id, chunk_text, embedding, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(source_type, source_id) DO UPDATE SET
                    chunk_text  = excluded.chunk_text,
                    embedding   = excluded.embedding,
                    updated_at  = excluded.updated_at
                """,
                (r["source_type"], r["source_id"],
                 r["chunk_text"], r["embedding"], r["updated_at"]),
                conn=conn,
            )
            n += 1
    return n


def main() -> None:
    legacy = sys.argv[1] if len(sys.argv) > 1 else None
    n = seed_from_legacy(legacy)
    print(f"seeded {n} skill embeddings row(s)")


if __name__ == "__main__":
    main()
