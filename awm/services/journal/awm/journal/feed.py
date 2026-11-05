"""The journal's change feed for the search archive.

``search_feed`` holds one row per thread first post with the sequence number of its
last change. Triggers on posts, tags, citations and tag names bump the root in the
same transaction as the write, so no code path can change a thread without the feed
showing it. A bump that touches many roots at once, such as a tag rename, gives them
one number, so the cursor is the pair ``(seq, root_id)``.
"""

from __future__ import annotations

import hashlib
import uuid
from typing import Any

from awm.journal import tags
from awm.journal.channel import cites_of_posts
from awm.journal.dao import JournalDAO
from awm.journal.search_index import INDEXED_PARAMS, INDEXED_SQL, thread_text
from awm.persistence.timestamps import ms_to_iso

DEFAULT_LIMIT = 200
MAX_LIMIT = 1000
_TITLE_CHARS = 120
_CHANNEL = "project:"


def _epoch(dao: JournalDAO, exclude: tuple[str, list]) -> str:
    row = dao.query_one("SELECT epoch FROM search_feed_epoch")
    if row is None:
        with dao.transaction() as conn:
            conn.execute("INSERT INTO search_feed_epoch (epoch) SELECT ? WHERE NOT EXISTS"
                         " (SELECT 1 FROM search_feed_epoch)", (uuid.uuid4().hex,))
        row = dao.query_one("SELECT epoch FROM search_feed_epoch")
    hide = hashlib.sha1(repr(exclude).encode()).hexdigest()[:12]
    return f"{row['epoch']}.{hide}"


def _title(meta: dict, body: str) -> str:
    title = meta.get("title")
    if isinstance(title, str) and title.strip():
        return title.strip()
    first = next((ln.strip() for ln in body.splitlines() if ln.strip()), "")
    return first[:_TITLE_CHARS]


def _entry(row, tag_ids: list[str], cites: list[str], conn) -> dict[str, Any]:
    from awm.journal.channel import _coerce_meta
    body = row["body"] or ""
    project, scope = row["owner_project"], row["owner_scope"]
    if not project and scope.startswith(_CHANNEL):
        project, scope = scope[len(_CHANNEL):], ""
    return {"title": _title(_coerce_meta(row["meta"]), body), "text": thread_text(conn, row["id"], body),
            "ts": ms_to_iso(row["ts"]) or "", "project": [project] if project else [],
            "scope": [scope] if scope else [], "tag": tags.names_of(tag_ids), "replies_to": cites}


def parse_cursor(since: str | None) -> tuple[int, str]:
    if not since:
        return 0, ""
    seq, _, root = str(since).partition(":")
    if not seq.isdigit():
        raise ValueError(f"since {since!r} is not a cursor this feed gave")
    return int(seq), root


def changes(since: str | None = None, limit: int | None = None,
            exclude: tuple[str, list] = ("", [])) -> dict[str, Any]:
    """The threads changed after ``since``: ``{changes: [{id, entry?}], next, done, epoch}``.

    A change without ``entry`` is a deletion: the thread is gone, stopped qualifying for the
    index, or ``exclude`` (a WHERE fragment from :func:`awm.journal.private.exclusion`) hides it.
    ``reset`` asks the reader to start over: its cursor is past the feed's head.
    """
    seq, root = parse_cursor(since)
    size = max(1, min(int(DEFAULT_LIMIT if limit is None else limit), MAX_LIMIT))
    dao = JournalDAO()
    epoch = _epoch(dao, exclude)
    head = dao.query_one("SELECT COALESCE(MAX(seq), 0) AS m FROM search_feed")["m"]
    if seq > head:
        return {"changes": [], "next": None, "done": True, "epoch": epoch, "reset": True}
    rows = dao.query_all("SELECT root_id, seq FROM search_feed WHERE seq > ? OR (seq = ? AND root_id > ?)"
                         " ORDER BY seq, root_id LIMIT ?", (seq, seq, root, size + 1))
    done = len(rows) <= size
    rows = rows[:size]
    ids = [r["root_id"] for r in rows]
    hide, hide_params = exclude
    live = {}
    if ids:
        marks = ",".join("?" * len(ids))
        live = {r["id"]: r for r in dao.query_all(
            f"SELECT id, owner_project, owner_scope, body, meta, ts FROM scope_posts"
            f" WHERE id IN ({marks}) AND body != ''{INDEXED_SQL}{hide}",
            [*ids, *INDEXED_PARAMS, *hide_params])}
    kept = list(live)
    by_post, cites = tags.of_posts(kept), cites_of_posts(kept)
    out = []
    with dao.transaction() as conn:
        for i in ids:
            row = live.get(i)
            out.append({"id": i} if row is None else
                       {"id": i, "entry": _entry(row, by_post[i], cites[i], conn)})
    last = rows[-1] if rows else None
    return {"changes": out, "next": f"{last['seq']}:{last['root_id']}" if last else (since or None),
            "done": done, "epoch": epoch}
