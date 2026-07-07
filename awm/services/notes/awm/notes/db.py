"""SQLite schema + low-level helpers for the notes DB.

The service owns its DB (``AWM_DIR/services/notes/notes.db``). ``NOTES_DDL`` is
this service's own-table DDL; ``dao.py`` appends ``EMBEDDINGS_DDL`` and hands the
whole thing to ``init_service_db``.

Data model:
  - ``notes`` — one row per note. ``id`` is a uuid that is ALSO the filename stem
    (``<id>.md``). ``path`` is the human title-as-path (``a/b/c``); it is NOT
    unique — the tree keys leaves by id, so two notes may share a title.
    ``deleted_at`` NULL = active; an ISO timestamp = in the trash.
  - ``notes_fts`` — FTS5 mirror of (path, content) for keyword search. The file
    is the source of truth; this is a derived index refreshed on every save.
  - ``vocab`` — the custom dictation vocabulary (whisper hotwords), a flat term
    set the editor sends up with each dictation session.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from datetime import datetime, timezone
from typing import Any

NOTES_DDL = """\
CREATE TABLE IF NOT EXISTS notes (
    id            TEXT PRIMARY KEY,   -- uuid; also the filename stem (<id>.md)
    path          TEXT NOT NULL,      -- title-as-path, e.g. "research/avarice/log"
    created       TEXT NOT NULL,
    modified      TEXT NOT NULL,
    deleted_at    TEXT,               -- ISO ts when trashed; NULL = active
    content_hash  TEXT,
    embedded_hash TEXT                -- content_hash at last embed; NULL = stale
);
CREATE INDEX IF NOT EXISTS idx_notes_path ON notes(path);
CREATE INDEX IF NOT EXISTS idx_notes_deleted ON notes(deleted_at);

CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts USING fts5(
    note_id UNINDEXED, path, content
);

CREATE TABLE IF NOT EXISTS vocab (
    term    TEXT PRIMARY KEY,
    created TEXT NOT NULL
);
"""

_WS = re.compile(r"\s+")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def normalize(text: str) -> str:
    return _WS.sub(" ", text.lower()).strip()


def content_hash(text: str) -> str:
    return hashlib.sha1(normalize(text).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Row access
# ---------------------------------------------------------------------------


def get_note(conn: sqlite3.Connection, note_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM notes WHERE id=?", (note_id,)).fetchone()


def list_notes(conn: sqlite3.Connection, *, include_trashed: bool = False) -> list[sqlite3.Row]:
    if include_trashed:
        sql = "SELECT * FROM notes ORDER BY path, modified DESC"
    else:
        sql = "SELECT * FROM notes WHERE deleted_at IS NULL ORDER BY path, modified DESC"
    return conn.execute(sql).fetchall()


def upsert_note(conn: sqlite3.Connection, row: dict[str, Any]) -> None:
    conn.execute(
        """
        INSERT INTO notes (id, path, created, modified, deleted_at,
                           content_hash, embedded_hash)
        VALUES (:id, :path, :created, :modified, :deleted_at,
                :content_hash, :embedded_hash)
        ON CONFLICT(id) DO UPDATE SET
            path=excluded.path,
            modified=excluded.modified,
            deleted_at=excluded.deleted_at,
            content_hash=excluded.content_hash,
            embedded_hash=excluded.embedded_hash
        """,
        row,
    )


def delete_note_row(conn: sqlite3.Connection, note_id: str) -> None:
    conn.execute("DELETE FROM notes WHERE id=?", (note_id,))


# ---------------------------------------------------------------------------
# FTS
# ---------------------------------------------------------------------------


def fts_replace(conn: sqlite3.Connection, note_id: str, path: str, content: str) -> None:
    conn.execute("DELETE FROM notes_fts WHERE note_id=?", (note_id,))
    conn.execute(
        "INSERT INTO notes_fts (note_id, path, content) VALUES (?,?,?)",
        (note_id, path, content),
    )


def fts_delete(conn: sqlite3.Connection, note_id: str) -> None:
    conn.execute("DELETE FROM notes_fts WHERE note_id=?", (note_id,))


# ---------------------------------------------------------------------------
# Vocab (custom dictation terms)
# ---------------------------------------------------------------------------


def vocab_terms(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute("SELECT term FROM vocab ORDER BY term COLLATE NOCASE").fetchall()
    return [r["term"] for r in rows]


def vocab_add(conn: sqlite3.Connection, term: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO vocab (term, created) VALUES (?,?)",
        (term.strip(), now_iso()),
    )


def vocab_remove(conn: sqlite3.Connection, term: str) -> None:
    conn.execute("DELETE FROM vocab WHERE term=?", (term.strip(),))
