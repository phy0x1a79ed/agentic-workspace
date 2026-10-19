"""The decisions' page of documents for the search archive.

Active decisions go out in ascending id order, keyed by the last id sent, so an
edit between pages never repeats or skips a surviving decision. The final page
also carries the full id set, which lets search drop what vanished. This reads
the tables directly and never records a sighting: ``seen_count`` ranks what
agents were shown, and a sync pass must not inflate it.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

from . import db, store

DEFAULT_LIMIT = 200
MAX_LIMIT = 1000
_TITLE_CHARS = 120


def _iso(stamp: str | None) -> str:
    moment = store._parse_ts(stamp)
    return moment.astimezone(timezone.utc).isoformat() if moment else ""


def _document(conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    parts = [f"Context: {row['context']}", f"Question: {row['question']}", f"Decision: {row['decision']}"]
    tags = db.tags_for(conn, row["id"])
    if tags:
        parts.append("Tags: " + ", ".join(tags))
    parts += [f"Note ({n['kind']}): {n['body']}" for n in db.notes_for(conn, row["id"])]
    doc = {"id": row["id"], "title": " ".join(row["question"].split())[:_TITLE_CHARS],
           "text": "\n\n".join(parts), "ts": _iso(row["created"] or row["added"]), "kind": "decision"}
    if tags:
        doc["tags"] = tags
    return doc


_VISIBLE = "status = 'active' AND id NOT IN (SELECT value FROM json_each(?))"


def export_page(conn: sqlite3.Connection, cursor: str | None = None, limit: int | None = None,
                exclude: frozenset[str] | set[str] = frozenset()) -> dict:
    """One page of ``{id,title,text,ts,kind,tags?}`` documents after ``cursor``, leaving out ``exclude``."""
    size = max(1, min(int(DEFAULT_LIMIT if limit is None else limit), MAX_LIMIT))
    hidden = json.dumps(sorted(exclude))
    rows = conn.execute(
        f"SELECT * FROM decisions WHERE {_VISIBLE} AND id > ? ORDER BY id LIMIT ?",
        (hidden, cursor or "", size + 1)).fetchall()
    done = len(rows) <= size
    rows = rows[:size]
    page = {"documents": [_document(conn, r) for r in rows],
            "next_cursor": None if done else rows[-1]["id"], "done": done}
    if done:
        page["ids"] = [r[0] for r in conn.execute(
            f"SELECT id FROM decisions WHERE {_VISIBLE} ORDER BY id", (hidden,))]
    return page
