"""The writing corpus's page of documents for the search archive.

Samples that are not marked duplicates go out in ascending id order, keyed by
the last id sent, so an edit between pages never repeats or skips a surviving
sample. The final page also carries the full id set, which lets search drop
what vanished.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any

DEFAULT_LIMIT = 200
MAX_LIMIT = 1000


def _iso(stamp: str | None) -> str:
    try:
        moment = datetime.fromisoformat(str(stamp).strip())
    except ValueError:
        return ""
    return (moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)).astimezone(timezone.utc).isoformat()


def _document(row: sqlite3.Row) -> dict[str, Any]:
    doc = {"id": row["id"], "title": row["name"], "text": row["content"] or "",
           "ts": _iso(row["modified"] or row["created"])}
    if row["type"]:
        doc["kind"] = row["type"]
    if row["web_link"]:
        doc["url"] = row["web_link"]
    return doc


def export_page(conn: sqlite3.Connection, cursor: str | None = None, limit: int | None = None) -> dict:
    """One page of ``{id,title,text,ts,kind,url}`` documents after ``cursor``."""
    size = max(1, min(int(DEFAULT_LIMIT if limit is None else limit), MAX_LIMIT))
    rows = conn.execute(
        "SELECT * FROM samples WHERE dup_of IS NULL AND id > ? ORDER BY id LIMIT ?",
        (cursor or "", size + 1)).fetchall()
    done = len(rows) <= size
    rows = rows[:size]
    page = {"documents": [_document(r) for r in rows],
            "next_cursor": None if done else rows[-1]["id"], "done": done}
    if done:
        page["ids"] = [r[0] for r in conn.execute("SELECT id FROM samples WHERE dup_of IS NULL ORDER BY id")]
    return page
