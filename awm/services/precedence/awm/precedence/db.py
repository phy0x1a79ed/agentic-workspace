"""SQLite schema + low-level row/tag/note/FTS helpers for the precedence DB.

The service owns its DB (``AWM_DIR/services/precedence/precedence.db``). A decision
is the free-text triple (context, question, decision) plus timestamps, a
supersede/status lifecycle, provenance, and the reputation columns (votes + seen
tracking). Notes attach to a decision as "something changed that may affect this
outcome" signals; tags are a flat, open set for AND-filtering across arbitrary
domains. ``PRECEDENCE_DDL`` is this service's own-table DDL; ``dao.py`` appends
``EMBEDDINGS_DDL`` and hands the whole thing to ``init_service_db``.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from typing import Any

# The precedence service's own tables (embeddings table is appended in dao.py).
PRECEDENCE_DDL = """\
CREATE TABLE IF NOT EXISTS decisions (
    id            TEXT PRIMARY KEY,
    context       TEXT NOT NULL,   -- the situation the preference applies to
    question      TEXT NOT NULL,   -- the decision point that arose
    decision      TEXT NOT NULL,   -- what was decided / preferred
    created       TEXT,            -- when the preference was formed (may be backdated)
    added         TEXT,            -- row birth (when this entry entered the archive)
    status        TEXT NOT NULL DEFAULT 'active',   -- active | superseded | retired
    superseded_by TEXT,            -- id of the replacing decision, if superseded
    source        TEXT,            -- manual | agent | memory | scope_post | journal | transcript
    source_ref    TEXT,            -- provenance pointer (file, post id, transcript uuid, ...)
    content_hash  TEXT,            -- hash of the normalized triple
    embedded_hash TEXT,            -- content_hash at last embed; NULL = not embedded
    upvotes       INTEGER NOT NULL DEFAULT 0,
    downvotes     INTEGER NOT NULL DEFAULT 0,
    seen_count    INTEGER NOT NULL DEFAULT 0,
    last_seen     TEXT
);
CREATE INDEX IF NOT EXISTS idx_decisions_status ON decisions(status);
CREATE INDEX IF NOT EXISTS idx_decisions_created ON decisions(created);

CREATE TABLE IF NOT EXISTS notes (
    id          TEXT PRIMARY KEY,
    decision_id TEXT NOT NULL,
    body        TEXT NOT NULL,
    kind        TEXT NOT NULL DEFAULT 'comment',  -- amendment | context-change | supersede | comment
    author      TEXT,
    created     TEXT,
    FOREIGN KEY (decision_id) REFERENCES decisions(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_notes_decision ON notes(decision_id);

CREATE TABLE IF NOT EXISTS tags (
    decision_id TEXT NOT NULL,
    tag         TEXT NOT NULL,
    PRIMARY KEY (decision_id, tag),
    FOREIGN KEY (decision_id) REFERENCES decisions(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_tags_tag ON tags(tag);

CREATE VIRTUAL TABLE IF NOT EXISTS decisions_fts USING fts5(
    decision_id UNINDEXED, context, question, decision
);
"""

_WS = re.compile(r"\s+")


def normalize(text: str) -> str:
    """Lowercase + collapse whitespace — basis for content hashing."""
    return _WS.sub(" ", (text or "").lower()).strip()


def content_hash(context: str, question: str, decision: str) -> str:
    """Stable hash of the normalized triple; drives the embedded_hash freshness check."""
    joined = "\x1f".join(normalize(t) for t in (context, question, decision))
    return hashlib.sha1(joined.encode("utf-8")).hexdigest()


def stable_id(*parts: str) -> str:
    """A short, deterministic id from arbitrary parts (source_ref or the triple).

    Used so ``import`` is idempotent on a stable key (a re-run doesn't duplicate),
    and an agent ``add`` of identical content collapses to one row.
    """
    joined = "\x1f".join(normalize(p) for p in parts)
    return hashlib.sha1(joined.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Decision row helpers
# ---------------------------------------------------------------------------

DECISION_COLS = [
    "id", "context", "question", "decision", "created", "added", "status",
    "superseded_by", "source", "source_ref", "content_hash", "embedded_hash",
    "upvotes", "downvotes", "seen_count", "last_seen",
]


def upsert_decision(conn: sqlite3.Connection, row: dict[str, Any]) -> None:
    """Insert or update a decision by id. Only the supplied columns are written.

    Reputation columns (votes / seen tracking) are intentionally NOT overwritten
    on update unless the caller passes them, so a re-import never resets a row's
    accumulated feedback.
    """
    cols = [c for c in DECISION_COLS if c in row]
    placeholders = ",".join("?" for _ in cols)
    updates = ",".join(f"{c}=excluded.{c}" for c in cols if c != "id")
    conn.execute(
        f"INSERT INTO decisions ({','.join(cols)}) VALUES ({placeholders}) "
        f"ON CONFLICT(id) DO UPDATE SET {updates}",
        [row[c] for c in cols],
    )


def get_decision(conn: sqlite3.Connection, did: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM decisions WHERE id=?", (did,)).fetchone()


def all_decisions(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM decisions ORDER BY created").fetchall()


def delete_decision(conn: sqlite3.Connection, did: str) -> None:
    fts_delete(conn, did)
    conn.execute("DELETE FROM notes WHERE decision_id=?", (did,))
    conn.execute("DELETE FROM tags WHERE decision_id=?", (did,))
    conn.execute("DELETE FROM decisions WHERE id=?", (did,))


# ---------------------------------------------------------------------------
# Tag helpers (flat, open vocabulary)
# ---------------------------------------------------------------------------


def set_tag(conn: sqlite3.Connection, did: str, tag: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO tags (decision_id, tag) VALUES (?,?)", (did, tag)
    )


def clear_tag(conn: sqlite3.Connection, did: str, tag: str) -> None:
    conn.execute("DELETE FROM tags WHERE decision_id=? AND tag=?", (did, tag))


def tags_for(conn: sqlite3.Connection, did: str) -> list[str]:
    rows = conn.execute(
        "SELECT tag FROM tags WHERE decision_id=? ORDER BY tag", (did,)
    ).fetchall()
    return [r["tag"] for r in rows]


# ---------------------------------------------------------------------------
# Note helpers
# ---------------------------------------------------------------------------


def add_note(
    conn: sqlite3.Connection, note_id: str, did: str, body: str,
    kind: str, author: str | None, created: str,
) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO notes (id, decision_id, body, kind, author, created) "
        "VALUES (?,?,?,?,?,?)",
        (note_id, did, body, kind, author, created),
    )


def notes_for(conn: sqlite3.Connection, did: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT id, body, kind, author, created FROM notes "
        "WHERE decision_id=? ORDER BY created, id",
        (did,),
    ).fetchall()
    return [
        {"id": r["id"], "body": r["body"], "kind": r["kind"],
         "author": r["author"], "created": r["created"]}
        for r in rows
    ]


# ---------------------------------------------------------------------------
# FTS helpers (keyword search over the triple)
# ---------------------------------------------------------------------------


def fts_replace(
    conn: sqlite3.Connection, did: str, context: str, question: str, decision: str
) -> None:
    conn.execute("DELETE FROM decisions_fts WHERE decision_id=?", (did,))
    conn.execute(
        "INSERT INTO decisions_fts (decision_id, context, question, decision) "
        "VALUES (?,?,?,?)",
        (did, context or "", question or "", decision or ""),
    )


def fts_delete(conn: sqlite3.Connection, did: str) -> None:
    conn.execute("DELETE FROM decisions_fts WHERE decision_id=?", (did,))
