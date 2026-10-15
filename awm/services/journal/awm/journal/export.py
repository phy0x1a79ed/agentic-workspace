"""The journal's page of documents for the search archive.

Posts of the indexed kinds go out in ascending id order, keyed by the last id
sent, so an edit between pages never repeats or skips a surviving post. The
final page also carries the full id set, which lets search drop what vanished.
"""

from __future__ import annotations

from awm.journal.channel import _coerce_meta
from awm.journal.dao import JournalDAO
from awm.journal.refs import ms_to_iso
from awm.journal.search_index import INDEXED_KINDS

DEFAULT_LIMIT = 200
MAX_LIMIT = 1000
_TITLE_CHARS = 120


def _title(meta: dict, body: str) -> str:
    title = meta.get("title")
    if isinstance(title, str) and title.strip():
        return title.strip()
    first = next((ln.strip() for ln in body.splitlines() if ln.strip()), "")
    return first[:_TITLE_CHARS]


def _document(row) -> dict:
    body = row["body"] or ""
    return {
        "id": row["id"], "title": _title(_coerce_meta(row["meta"]), body), "text": body,
        "ts": ms_to_iso(row["ts"]) or "", "project": row["owner_project"],
        "scope": row["owner_scope"], "kind": row["kind"],
    }


def export_page(cursor: str | None = None, limit: int | None = None) -> dict:
    """One page of ``{id,title,text,ts,project,scope,kind}`` documents after ``cursor``."""
    size = max(1, min(int(DEFAULT_LIMIT if limit is None else limit), MAX_LIMIT))
    marks = ",".join("?" * len(INDEXED_KINDS))
    dao = JournalDAO()
    rows = dao.query_all(
        f"SELECT id, owner_project, owner_scope, kind, body, meta, ts FROM scope_posts"
        f" WHERE kind IN ({marks}) AND body != '' AND id > ? ORDER BY id LIMIT ?",
        [*INDEXED_KINDS, cursor or "", size + 1])
    done = len(rows) <= size
    rows = rows[:size]
    page = {"documents": [_document(r) for r in rows],
            "next_cursor": None if done else rows[-1]["id"], "done": done}
    if done:
        page["ids"] = [r["id"] for r in dao.query_all(
            f"SELECT id FROM scope_posts WHERE kind IN ({marks}) AND body != '' ORDER BY id",
            list(INDEXED_KINDS))]
    return page
