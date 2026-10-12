"""The notes' page of documents for the search archive.

Live notes go out in ascending id order, keyed by the last id sent, so an edit
between pages never repeats or skips a surviving note. Trashed notes stay out:
the 30-day trash is not part of what search should find, and a note that is
trashed drops from the index at the next full pass. The final page also carries
the full id set, which lets search drop what vanished.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from . import config, rooms

DEFAULT_LIMIT = 200
MAX_LIMIT = 1000


def _document(row: sqlite3.Row) -> dict[str, Any]:
    return {"id": row["id"], "title": row["path"], "text": rooms.live_text(row["id"]),
            "ts": row["modified"] or row["created"] or ""}


def export_page(conn: sqlite3.Connection, cursor: str | None = None, limit: int | None = None) -> dict:
    """One page of ``{id,title,text,ts}`` documents after ``cursor``.

    Raises when notes exist and their file store holds no files: a missing file
    reads as an empty note, and a wiped store must not pass for a corpus of
    empty notes.
    """
    size = max(1, min(int(DEFAULT_LIMIT if limit is None else limit), MAX_LIMIT))
    rows = conn.execute(
        "SELECT id, path, created, modified FROM notes WHERE deleted_at IS NULL AND id > ?"
        " ORDER BY id LIMIT ?", (cursor or "", size + 1)).fetchall()
    if rows and next(config.files_dir().glob("*.md"), None) is None:
        raise FileNotFoundError(f"the note files directory {config.files_dir()} holds no notes")
    done = len(rows) <= size
    rows = rows[:size]
    page = {"documents": [_document(r) for r in rows],
            "next_cursor": None if done else rows[-1]["id"], "done": done}
    if done:
        page["ids"] = [r[0] for r in conn.execute(
            "SELECT id FROM notes WHERE deleted_at IS NULL ORDER BY id")]
    return page
