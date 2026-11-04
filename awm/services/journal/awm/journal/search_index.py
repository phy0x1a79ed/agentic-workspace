"""The journal's search index: posts in the retrieval engine.

Writes go through the service's one index worker (:class:`ServiceIndex`). A
supervised backfill walks the post table at startup and every few hours,
because rows written by a seed or an older release never passed through the
post hook.

One document stands for one thread: its first post, with each comment's text
after it, so a hit on a comment finds the post it answers (fleet.md R107 item 5).
"""

from __future__ import annotations

import json
from concurrent.futures import Future
from typing import Any, Iterable

from awm.journal import builtin_tags
from awm.persistence import embeddings as engine
from awm.persistence.service_index import ServiceIndex
from awm.persistence.timestamps import ms_to_date

SERVICE = "journal"
#: A thread is indexed when its first post carries one of these tags.
INDEXED_TAGS = (builtin_tags.JOURNAL, builtin_tags.MESSAGE, builtin_tags.PRECEDENT)
_MARKS = ",".join("?" * len(INDEXED_TAGS))
#: A WHERE fragment over ``scope_posts`` keeping the thread first posts the index holds.
INDEXED_SQL = (f" AND thread_id IS NULL AND EXISTS (SELECT 1 FROM post_tags it WHERE"
               f" it.post_id = scope_posts.id AND it.tag_id IN ({_MARKS}))"
               f" AND NOT EXISTS (SELECT 1 FROM post_tags ft WHERE"
               f" ft.post_id = scope_posts.id AND ft.tag_id = ?)")
INDEXED_PARAMS = [*INDEXED_TAGS, builtin_tags.FOLDED]


def may_hold(terms: Any) -> bool:
    """Whether a tag filter can match a thread the index holds.

    A filter that requires a kind tag outside :data:`INDEXED_TAGS`, such as
    ``brief``, cannot, and its search falls back to a substring match.
    """
    from awm.journal import tags
    for negated, name in tags.parse_filter(terms):
        tag = tags.by_name(name)
        if (not negated and tag is not None and tag.id not in INDEXED_TAGS
                and (tag.id in builtin_tags.KIND_TAGS or tag.id.startswith(builtin_tags.MIGRATED_PREFIX))):
            return False
    return True


def thread_text(conn, root_id: str, body: str) -> str:
    """A thread's first post with each comment's text after it, oldest first."""
    parts = [body or ""]
    parts += [r["body"] for r in conn.execute(
        "SELECT body FROM scope_posts WHERE thread_id=? AND body != '' ORDER BY ts, id",
        (root_id,))]
    return "\n\n".join(p for p in parts if p)


def post_document(row, conn=None) -> engine.Document:
    try:
        meta = json.loads(row["meta"] or "{}")
    except (TypeError, ValueError):
        meta = {}
    title = meta.get("title") if isinstance(meta, dict) else ""
    text = thread_text(conn, row["id"], row["body"]) if conn is not None else row["body"] or ""
    return engine.Document(
        row["id"], text, title=str(title or ""),
        context=f"{row['owner_project']}/{row['owner_scope']}", date=ms_to_date(row["ts"]))


def _post_docs(conn) -> Iterable[engine.Document]:
    docs = [post_document(r, conn) for r in conn.execute(
        f"SELECT id, owner_project, owner_scope, body, meta, ts FROM scope_posts"
        f" WHERE 1=1{INDEXED_SQL}", INDEXED_PARAMS)]
    return [d for d in docs if d.body]


def _index_post(conn, post_id: str) -> None:
    row = conn.execute(
        f"SELECT id, owner_project, owner_scope, body, meta, ts FROM scope_posts WHERE id=?"
        f"{INDEXED_SQL}", (post_id, *INDEXED_PARAMS)).fetchone()
    doc = post_document(row, conn) if row is not None else None
    if doc is None or not doc.body:
        engine.delete_document(conn, "post", post_id)
    else:
        engine.index_document(conn, "post", doc)


INDEX = ServiceIndex(SERVICE, lambda conn: {"post": _post_docs(conn)})
flush, ensure_schema, run_reindex = INDEX.flush, INDEX.ensure_schema, INDEX.run_reindex
backfill_loop, search = INDEX.backfill_loop, INDEX.search


def index_post(post_id: str) -> Future:
    """Queue a reindex of the thread whose first post is ``post_id``."""
    return INDEX.submit(_index_post, post_id)
