"""The journal's search index: posts in the retrieval engine.

Writes go through one background worker, so a post returns without waiting
for its embedding and the model is never loaded twice. A supervised backfill
walks the post table at startup and every few hours, because rows written by a
seed or an older release never passed through the post hook.

One document stands for one thread: its first post, with each comment's text
after it, so a hit on a comment finds the post it answers (fleet.md R107 item 5).
"""

from __future__ import annotations

import asyncio
import json
import logging
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from awm.journal import kinds
from awm.persistence import embeddings as engine
from awm.persistence.databases import get_connection, get_connection_at, service_db_path

log = logging.getLogger("awm.journal.search_index")

SERVICE = "journal"
#: A thread is indexed when its first post carries one of these tags.
INDEXED_TAGS = (kinds.JOURNAL, kinds.MESSAGE, kinds.GOAL, kinds.PRECEDENT,
                kinds.MIGRATED_PREFIX + "debrief", kinds.MIGRATED_PREFIX + "note")
_MARKS = ",".join("?" * len(INDEXED_TAGS))
#: A WHERE fragment over ``scope_posts`` keeping the thread first posts the index holds.
INDEXED_SQL = (f" AND thread_id IS NULL AND EXISTS (SELECT 1 FROM post_tags it WHERE"
               f" it.post_id = scope_posts.id AND it.tag_id IN ({_MARKS}))")
INDEXED_PARAMS = list(INDEXED_TAGS)
BACKFILL_EVERY_S = 6 * 3600

_worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="journal-index")


def _date(ts_ms: int | None) -> str:
    if not ts_ms:
        return ""
    return datetime.fromtimestamp(ts_ms / 1000, timezone.utc).date().isoformat()


def may_hold(terms: Any) -> bool:
    """Whether a tag filter can match a thread the index holds.

    A filter that requires a kind tag outside :data:`INDEXED_TAGS`, such as
    ``brief``, cannot, and its search falls back to a substring match.
    """
    from awm.journal import tags
    for negated, name in tags.parse_filter(terms):
        tag = tags.by_name(name)
        if (not negated and tag is not None and tag.id not in INDEXED_TAGS
                and (tag.id in kinds.KIND_TAGS or tag.id.startswith(kinds.MIGRATED_PREFIX))):
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
        context=f"{row['owner_project']}/{row['owner_scope']}", date=_date(row["ts"]))


def _post_docs(conn) -> Iterable[engine.Document]:
    docs = [post_document(r, conn) for r in conn.execute(
        f"SELECT id, owner_project, owner_scope, body, meta, ts FROM scope_posts"
        f" WHERE 1=1{INDEXED_SQL}", INDEXED_PARAMS)]
    return [d for d in docs if d.body]


def _run(db: Path, fn, *args) -> None:
    conn = get_connection_at(db)
    try:
        fn(conn, *args)
    except Exception:
        log.exception("search index update failed: %s%r", fn.__name__, args)
    finally:
        conn.close()


def _index_post(conn, post_id: str) -> None:
    row = conn.execute(
        f"SELECT id, owner_project, owner_scope, body, meta, ts FROM scope_posts WHERE id=?"
        f"{INDEXED_SQL}", (post_id, *INDEXED_PARAMS)).fetchone()
    doc = post_document(row, conn) if row is not None else None
    if doc is None or not doc.body:
        engine.delete_document(conn, "post", post_id)
    else:
        engine.index_document(conn, "post", doc)


# ``post_id`` is a thread's first post. Each job binds the DB when it is queued, not when it runs, so a write can
# never land in whatever database the process points at later.
def index_post(post_id: str) -> Future:
    return _worker.submit(_run, service_db_path(SERVICE), _index_post, post_id)


def flush() -> None:
    """Wait until every queued index update has been written."""
    _worker.submit(lambda: None).result()


def ensure_schema() -> None:
    conn = get_connection(SERVICE)
    try:
        engine.ensure_schema(conn)
    finally:
        conn.close()


def reindex(*, force: bool = False, dry_run: bool = False,
            db: Path | None = None) -> dict[str, Any]:
    """Bring the post index in line with the table; returns counts per type."""
    conn = get_connection_at(db or service_db_path(SERVICE))
    try:
        engine.ensure_schema(conn)
        return {"post": engine.reindex(conn, "post", _post_docs(conn), force=force,
                                       prune=True, dry_run=dry_run)}
    finally:
        conn.close()


def run_reindex(*, force: bool = False, dry_run: bool = False) -> dict[str, Any]:
    """:func:`reindex` on the index worker, so it never races the backfill."""
    return _worker.submit(reindex, force=force, dry_run=dry_run,
                          db=service_db_path(SERVICE)).result()


async def backfill_loop() -> None:
    """Reindex now and every :data:`BACKFILL_EVERY_S`; runs under ``spawn_supervised``."""
    loop = asyncio.get_running_loop()
    while True:
        counts = await loop.run_in_executor(_worker, reindex)
        log.info("search backfill: %s", {k: {x: v[x] for x in ("documents", "indexed", "pruned",
                                                               "seconds")}
                                         for k, v in counts.items()})
        await asyncio.sleep(BACKFILL_EVERY_S)


def search(source_type: str, query: str, *, allowed: str | None = None,
           params: Iterable[Any] = (), limit: int = 10) -> engine.SearchResult:
    conn = get_connection(SERVICE)
    try:
        return engine.search(conn, query, source_type=source_type, allowed=allowed,
                             params=params, limit=limit)
    finally:
        conn.close()
