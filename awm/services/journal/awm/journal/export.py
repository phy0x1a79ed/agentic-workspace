"""The journal's page of documents for the search archive.

One document stands for one thread: its first post, carrying an indexed tag,
with its comments' text after it, its comment count and the journals it cites
as ``reply_to``, which search's ``hop`` walks. Documents go out in
ascending id order, keyed by the last id sent, so an edit between pages never
repeats or skips a surviving post. The final page also carries the full id
set, which lets search drop what vanished.
"""

from __future__ import annotations

from awm.journal import tags
from awm.journal.channel import _coerce_meta, cites_of_posts, comment_counts
from awm.journal.dao import JournalDAO
from awm.persistence.timestamps import ms_to_iso
from awm.journal.indexed import INDEXED_PARAMS, INDEXED_SQL, thread_text

DEFAULT_LIMIT = 200
MAX_LIMIT = 1000
_TITLE_CHARS = 120


def _title(meta: dict, body: str) -> str:
    title = meta.get("title")
    if isinstance(title, str) and title.strip():
        return title.strip()
    first = next((ln.strip() for ln in body.splitlines() if ln.strip()), "")
    return first[:_TITLE_CHARS]


def _document(row, tag_ids: list[str], comments: int, cites: list[str], conn) -> dict:
    body = row["body"] or ""
    return {
        "id": row["id"], "title": _title(_coerce_meta(row["meta"]), body),
        "text": thread_text(conn, row["id"], body),
        "ts": ms_to_iso(row["ts"]) or "", "project": row["owner_project"],
        "scope": row["owner_scope"], "tags": tags.names_of(tag_ids),
        "kind": tags.derived_kind(tag_ids), "comments": comments, "reply_to": cites,
    }


def export_page(cursor: str | None = None, limit: int | None = None,
                exclude: tuple[str, list] = ("", [])) -> dict:
    """One page of ``{id,title,text,ts,project,scope,tags,kind,comments,reply_to}`` documents after ``cursor``.

    ``kind`` is derived from the tags for a search that still filters on it.

    ``exclude`` is a WHERE fragment from :func:`awm.journal.private.exclusion`.
    """
    hide, hide_params = exclude
    size = max(1, min(int(DEFAULT_LIMIT if limit is None else limit), MAX_LIMIT))
    dao = JournalDAO()
    rows = dao.query_all(
        f"SELECT id, owner_project, owner_scope, body, meta, ts FROM scope_posts"
        f" WHERE body != ''{INDEXED_SQL} AND id > ?{hide} ORDER BY id LIMIT ?",
        [*INDEXED_PARAMS, cursor or "", *hide_params, size + 1])
    done = len(rows) <= size
    rows = rows[:size]
    ids = [r["id"] for r in rows]
    by_post, counts, cites = tags.of_posts(ids), comment_counts(ids), cites_of_posts(ids)
    with dao.transaction() as conn:
        documents = [_document(r, by_post[r["id"]], counts[r["id"]], cites[r["id"]], conn) for r in rows]
    page = {"documents": documents,
            "next_cursor": None if done else rows[-1]["id"], "done": done}
    if done:
        page["ids"] = [r["id"] for r in dao.query_all(
            f"SELECT id FROM scope_posts WHERE body != ''{INDEXED_SQL}{hide} ORDER BY id",
            [*INDEXED_PARAMS, *hide_params])]
    return page
