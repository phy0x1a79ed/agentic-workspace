"""SQLite schema + low-level row/tag/FTS helpers for the writing corpus DB.

The service owns its DB (``AWM_DIR/services/writing/writing.db``). Unlike the
original file-backed corpus CLI, full sample text lives in-DB (``samples.content``)
so nothing reads the text corpus at query time; ``file`` is kept only as optional
provenance. ``WRITING_DDL`` is this service's own-table DDL; ``dao.py`` appends
``EMBEDDINGS_DDL`` and hands the whole thing to ``init_service_db``.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from typing import Any

from . import config

# The writing service's own tables (embeddings table is appended in dao.py).
WRITING_DDL = """\
CREATE TABLE IF NOT EXISTS samples (
    id            TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    file          TEXT,            -- optional provenance (original relative path)
    type          TEXT,
    created       TEXT,
    modified      TEXT,
    wordcount     INTEGER,
    note          TEXT,
    web_link      TEXT,
    content       TEXT,            -- full sample text, in-DB (no file dependency)
    content_hash  TEXT,
    embedded_hash TEXT,            -- content_hash at last embed; NULL = not embedded
    grade         TEXT,            -- mirror of the grade tag, for convenience
    dup_of        TEXT,            -- keeper id if this is a duplicate; NULL = keeper/unique
    dup_cluster   TEXT             -- shared cluster id for a dup group
);

CREATE TABLE IF NOT EXISTS tags (
    sample_id TEXT NOT NULL,
    facet     TEXT NOT NULL,
    value     TEXT NOT NULL,
    PRIMARY KEY (sample_id, facet, value),
    FOREIGN KEY (sample_id) REFERENCES samples(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_tags_facet_value ON tags(facet, value);

CREATE VIRTUAL TABLE IF NOT EXISTS samples_fts USING fts5(
    sample_id UNINDEXED, name, note, content
);
"""

_WS = re.compile(r"\s+")


def normalize(text: str) -> str:
    """Lowercase + collapse whitespace — basis for content hashing and dedup."""
    return _WS.sub(" ", text.lower()).strip()


def content_hash(text: str) -> str:
    return hashlib.sha1(normalize(text).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Tag helpers
# ---------------------------------------------------------------------------


def set_tag(conn: sqlite3.Connection, sample_id: str, facet: str, value: str) -> None:
    config.validate_tag(facet, value)
    if facet in config.SINGLE_VALUE_FACETS:
        conn.execute("DELETE FROM tags WHERE sample_id=? AND facet=?", (sample_id, facet))
        if facet == "grade":
            conn.execute("UPDATE samples SET grade=? WHERE id=?", (value, sample_id))
    conn.execute(
        "INSERT OR IGNORE INTO tags (sample_id, facet, value) VALUES (?,?,?)",
        (sample_id, facet, value),
    )


def clear_tag(conn: sqlite3.Connection, sample_id: str, facet: str, value: str) -> None:
    conn.execute(
        "DELETE FROM tags WHERE sample_id=? AND facet=? AND value=?",
        (sample_id, facet, value),
    )
    if facet == "grade":
        conn.execute("UPDATE samples SET grade=NULL WHERE id=?", (sample_id,))


def tags_for(conn: sqlite3.Connection, sample_id: str) -> list[tuple[str, str]]:
    rows = conn.execute(
        "SELECT facet, value FROM tags WHERE sample_id=? ORDER BY facet, value",
        (sample_id,),
    ).fetchall()
    return [(r["facet"], r["value"]) for r in rows]


def tags_str(conn: sqlite3.Connection, sample_id: str) -> str:
    return " ".join(f"{f}:{v}" for f, v in tags_for(conn, sample_id))


# ---------------------------------------------------------------------------
# FTS helpers
# ---------------------------------------------------------------------------


def fts_replace(conn: sqlite3.Connection, sample_id: str, name: str, note: str, content: str) -> None:
    conn.execute("DELETE FROM samples_fts WHERE sample_id=?", (sample_id,))
    conn.execute(
        "INSERT INTO samples_fts (sample_id, name, note, content) VALUES (?,?,?,?)",
        (sample_id, name or "", note or "", content or ""),
    )


def fts_delete(conn: sqlite3.Connection, sample_id: str) -> None:
    conn.execute("DELETE FROM samples_fts WHERE sample_id=?", (sample_id,))


# ---------------------------------------------------------------------------
# Sample row helpers
# ---------------------------------------------------------------------------

SAMPLE_COLS = [
    "id", "name", "file", "type", "created", "modified", "wordcount",
    "note", "web_link", "content", "content_hash", "embedded_hash", "grade",
    "dup_of", "dup_cluster",
]


def upsert_sample(conn: sqlite3.Connection, row: dict[str, Any]) -> None:
    cols = [c for c in SAMPLE_COLS if c in row]
    placeholders = ",".join("?" for _ in cols)
    updates = ",".join(f"{c}=excluded.{c}" for c in cols if c != "id")
    conn.execute(
        f"INSERT INTO samples ({','.join(cols)}) VALUES ({placeholders}) "
        f"ON CONFLICT(id) DO UPDATE SET {updates}",
        [row[c] for c in cols],
    )


def get_sample(conn: sqlite3.Connection, sample_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM samples WHERE id=?", (sample_id,)).fetchone()


def all_samples(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM samples ORDER BY created").fetchall()


def delete_sample(conn: sqlite3.Connection, sample_id: str) -> None:
    fts_delete(conn, sample_id)
    conn.execute("DELETE FROM tags WHERE sample_id=?", (sample_id,))
    conn.execute("DELETE FROM samples WHERE id=?", (sample_id,))
