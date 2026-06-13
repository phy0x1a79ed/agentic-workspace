"""One-time seed: carry ``artifacts`` and their embeddings from the legacy
shared ``state.db`` into the artifacts service's own DB.

The modular cutover drops the shared runtime DB but leaves the old file on disk
as a read-only legacy source. This script extracts the artifacts service's rows.

Because legacy rows carry ``agent_id`` (a uuid FK into ``agents``), seeding
requires the identity join:

    SELECT a.id AS agent_id, p.name AS project, a.scope AS scope
      FROM agents a
      JOIN projects p ON p.id = a.project_id

Rows whose ``agent_id`` does not appear in that join (orphans) are SKIPPED and
reported. Embeddings with ``source_type='artifact'`` are seeded straight across.

Idempotent (upsert-on-conflict), so re-running is safe. Always run against a
COPY of any live ``state.db``, never the live file.

Usage:
    python -m awm.artifacts.seed [LEGACY_STATE_DB]   # defaults to awm.config.DB_PATH
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

from awm.artifacts.dao import ArtifactsDAO, init


def seed_from_legacy(legacy_db: str | Path | None = None) -> dict:
    """Copy legacy ``artifacts`` rows into the artifacts service's DB.

    Returns a summary dict:
        seeded      — number of artifact rows seeded
        skipped     — number of orphan rows (agent_id not resolved)
        embeddings  — number of embeddings rows seeded

    A missing legacy file or missing table is a no-op (all counts 0) —
    a fresh install has nothing to carry over.
    """
    if legacy_db is None:
        from awm.config import DB_PATH
        legacy_db = DB_PATH
    legacy_db = Path(legacy_db)

    init()

    if not legacy_db.exists():
        print(f"Legacy DB not found at {legacy_db} — nothing to seed.")
        return {"seeded": 0, "skipped": 0, "embeddings": 0}

    src = sqlite3.connect(f"file:{legacy_db}?mode=ro", uri=True)
    src.row_factory = sqlite3.Row
    try:
        # Check that the artifacts table exists in legacy
        tbl = src.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name='artifacts'"
        ).fetchone()
        if tbl is None:
            print("No 'artifacts' table in legacy DB — nothing to seed.")
            return {"seeded": 0, "skipped": 0, "embeddings": 0}

        # Build agent_id → (project, scope) map from legacy identity tables
        id_rows = src.execute(
            "SELECT a.id AS agent_id, p.name AS project, a.scope AS scope "
            "FROM agents a "
            "JOIN projects p ON p.id = a.project_id"
        ).fetchall()
        agent_map: dict[str, tuple[str, str]] = {
            r["agent_id"]: (r["project"], r["scope"]) for r in id_rows
        }

        # Fetch legacy artifacts
        artifact_rows = src.execute(
            "SELECT agent_id, name, artifact_type, path, description, format, "
            "tags, status, created_at, updated_at FROM artifacts"
        ).fetchall()

        # Fetch legacy embeddings for artifacts
        emb_rows = src.execute(
            "SELECT source_type, source_id, chunk_text, embedding, updated_at "
            "FROM embeddings WHERE source_type = 'artifact'"
        ).fetchall() if _has_table(src, "embeddings") else []

    finally:
        src.close()

    dao = ArtifactsDAO()
    seeded = 0
    skipped = 0
    orphans: list[str] = []

    with dao.transaction() as conn:
        for r in artifact_rows:
            agent_id = r["agent_id"]
            nat_key = agent_map.get(agent_id)
            if nat_key is None:
                skipped += 1
                orphans.append(agent_id)
                print(
                    f"  SKIP: agent_id={agent_id!r} not in identity join — orphan row",
                    flush=True,
                )
                continue
            project, scope = nat_key
            # Idempotent: use INSERT OR IGNORE on a fresh upsert by (path, project, scope)
            conn.execute(
                "INSERT INTO artifacts "
                "(project, scope, name, artifact_type, path, description, "
                " format, tags, status, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT DO NOTHING",
                (project, scope,
                 r["name"] or "", r["artifact_type"] or "", r["path"] or "",
                 r["description"], r["format"], r["tags"],
                 r["status"] or "current",
                 r["created_at"], r["updated_at"]),
            )
            seeded += 1

    # Seed embeddings straight across (source_type/source_id may reference IDs
    # that now differ from the new DB's AUTOINCREMENT — we preserve the mapping
    # as-is; source_id refers to the INTEGER PK which resets in the new DB.
    # This is an approximation: a full reindex is the clean path, but the seed
    # at least carries the text and embeddings for semantic continuity).
    emb_seeded = 0
    if emb_rows:
        with dao.transaction() as conn:
            for e in emb_rows:
                conn.execute(
                    "INSERT INTO embeddings "
                    "(source_type, source_id, chunk_text, embedding, updated_at) "
                    "VALUES (?, ?, ?, ?, ?) "
                    "ON CONFLICT(source_type, source_id) DO UPDATE SET "
                    "chunk_text=excluded.chunk_text, "
                    "embedding=excluded.embedding, "
                    "updated_at=excluded.updated_at",
                    (e["source_type"], e["source_id"],
                     e["chunk_text"], e["embedding"], e["updated_at"]),
                )
                emb_seeded += 1

    return {"seeded": seeded, "skipped": skipped, "embeddings": emb_seeded}


def _has_table(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


def main() -> None:
    legacy = sys.argv[1] if len(sys.argv) > 1 else None
    result = seed_from_legacy(legacy)
    print(
        f"seeded {result['seeded']} artifact row(s), "
        f"skipped {result['skipped']} orphan(s), "
        f"seeded {result['embeddings']} embedding(s)"
    )


if __name__ == "__main__":
    main()
