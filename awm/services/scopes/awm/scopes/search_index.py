"""The scopes service's search index: posts, scopes and projects in the retrieval engine.

Writes go through one background worker, so a post returns without waiting
for its embedding and the model is never loaded twice. A supervised backfill
walks the tables at startup and every few hours, because rows written by a
seed or an older release never passed through the post hook.
"""

from __future__ import annotations

import asyncio
import json
import logging
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from awm.persistence import embeddings as engine
from awm.persistence.databases import get_connection, get_connection_at, service_db_path

log = logging.getLogger("awm.scopes.search_index")

INDEXED_KINDS = ("journal", "message", "goal", "debrief", "note")
BACKFILL_EVERY_S = 6 * 3600
_README_CHARS = 2000
_README_CHECKOUTS = ("release", "main", "dev")

_worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="scopes-index")


def _date(ts_ms: int | None) -> str:
    if not ts_ms:
        return ""
    return datetime.fromtimestamp(ts_ms / 1000, timezone.utc).date().isoformat()


def _read(path: Path, limit: int | None = None) -> str:
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return ""
    return text[:limit] if limit else text


# ---------------------------------------------------------------------------
# Documents
# ---------------------------------------------------------------------------


def post_document(row) -> engine.Document:
    try:
        meta = json.loads(row["meta"] or "{}")
    except (TypeError, ValueError):
        meta = {}
    title = meta.get("title") if isinstance(meta, dict) else ""
    return engine.Document(
        row["id"], row["body"] or "", title=str(title or ""),
        context=f"{row['owner_project']}/{row['owner_scope']}", date=_date(row["ts"]))


def scope_document(project: str, scope: str, worktree: str | None,
                   created_at: int | None) -> engine.Document:
    """The scope's goals in force and its ``.awm/context.md``."""
    from awm.scopes import goals
    from awm.scopes.scopes import _resolve_worktree

    objectives = [g.objective for g in goals.read_goals(project, scope, levels=["scope"])]
    context = _read(_resolve_worktree(project, scope, worktree) / ".awm" / "context.md")
    body = "\n\n".join(x for x in (*objectives, context) if x.strip())
    return engine.Document(f"{project}/{scope}", body, title=f"{project}/{scope}",
                           date=_date(created_at))


def project_document(name: str, scopes: Iterable[str]) -> engine.Document:
    """The project's scope names and the head of its README."""
    from awm.config import PROJECTS_DIR

    readme = ""
    for checkout in _README_CHECKOUTS:
        readme = _read(PROJECTS_DIR / name / checkout / "README.md", _README_CHARS)
        if readme:
            break
    names = sorted(set(scopes))
    body = "\n\n".join(x for x in (f"Scopes: {', '.join(names)}" if names else "", readme) if x)
    return engine.Document(name, body, title=name)


def _post_docs(conn) -> list[engine.Document]:
    marks = ",".join("?" * len(INDEXED_KINDS))
    return [post_document(r) for r in conn.execute(
        f"SELECT id, owner_project, owner_scope, body, meta, ts FROM scope_posts"
        f" WHERE kind IN ({marks}) AND body != ''", INDEXED_KINDS)]


def _scope_rows(conn, project: str | None = None, scope: str | None = None):
    sql = ("SELECT p.name AS project, a.scope, a.worktree, a.created_at"
           " FROM agents a JOIN projects p ON p.id = a.project_id WHERE 1=1")
    params: list = []
    if project is not None:
        sql += " AND p.name = ? AND a.scope = ?"
        params += [project, scope]
    latest: dict[tuple[str, str], Any] = {}
    for r in conn.execute(sql + " ORDER BY a.created_at", params):
        latest[(r["project"], r["scope"])] = r
    return latest.values()


def _scope_docs(conn) -> list[engine.Document]:
    return [scope_document(r["project"], r["scope"], r["worktree"], r["created_at"])
            for r in _scope_rows(conn)]


def _project_docs(conn) -> list[engine.Document]:
    from awm.config import PROJECTS_DIR

    scopes: dict[str, list[str]] = {}
    for r in conn.execute("SELECT p.name, a.scope FROM projects p"
                          " LEFT JOIN agents a ON a.project_id = p.id"):
        scopes.setdefault(r[0], []).extend([r[1]] if r[1] else [])
    if PROJECTS_DIR.is_dir():
        for child in PROJECTS_DIR.iterdir():
            if (child / ".bare").is_dir():
                scopes.setdefault(child.name, [])
    return [project_document(n, s) for n, s in sorted(scopes.items())]


# ---------------------------------------------------------------------------
# Writes (background)
# ---------------------------------------------------------------------------


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
        "SELECT id, owner_project, owner_scope, kind, body, meta, ts FROM scope_posts WHERE id=?",
        (post_id,)).fetchone()
    if row is None or row["kind"] not in INDEXED_KINDS or not row["body"]:
        engine.delete_document(conn, "post", post_id)
    else:
        engine.index_document(conn, "post", post_document(row))


def _index_scope(conn, project: str, scope: str) -> None:
    rows = list(_scope_rows(conn, project, scope))
    if not rows:
        engine.delete_document(conn, "scope", f"{project}/{scope}")
        return
    r = rows[-1]
    engine.index_document(conn, "scope", scope_document(project, scope, r["worktree"],
                                                        r["created_at"]))


def _index_project(conn, name: str) -> None:
    scopes = [r[0] for r in conn.execute(
        "SELECT a.scope FROM agents a JOIN projects p ON p.id = a.project_id WHERE p.name=?",
        (name,))]
    engine.index_document(conn, "project", project_document(name, scopes))


# Each job binds the DB when it is queued, not when it runs, so a write can
# never land in whatever database the process points at later.
def index_post(post_id: str) -> Future:
    return _worker.submit(_run, service_db_path("scopes"), _index_post, post_id)


def index_scope(project: str, scope: str) -> Future:
    return _worker.submit(_run, service_db_path("scopes"), _index_scope, project, scope)


def index_project(name: str) -> Future:
    return _worker.submit(_run, service_db_path("scopes"), _index_project, name)


def flush() -> None:
    """Wait until every queued index update has been written."""
    _worker.submit(lambda: None).result()


# ---------------------------------------------------------------------------
# Backfill
# ---------------------------------------------------------------------------


def ensure_schema() -> None:
    conn = get_connection("scopes")
    try:
        engine.ensure_schema(conn)
    finally:
        conn.close()


def reindex(*, force: bool = False, dry_run: bool = False,
            db: Path | None = None) -> dict[str, Any]:
    """Bring posts, scopes and projects in line with the tables; returns counts per type."""
    conn = get_connection_at(db or service_db_path("scopes"))
    try:
        engine.ensure_schema(conn)
        return {
            st: engine.reindex(conn, st, docs(conn), force=force, prune=True, dry_run=dry_run)
            for st, docs in (("post", _post_docs), ("scope", _scope_docs),
                             ("project", _project_docs))
        }
    finally:
        conn.close()


def run_reindex(*, force: bool = False, dry_run: bool = False) -> dict[str, Any]:
    """:func:`reindex` on the index worker, so it never races the backfill."""
    return _worker.submit(reindex, force=force, dry_run=dry_run,
                          db=service_db_path("scopes")).result()


async def backfill_loop() -> None:
    """Reindex now and every :data:`BACKFILL_EVERY_S`; runs under ``spawn_supervised``."""
    loop = asyncio.get_running_loop()
    while True:
        counts = await loop.run_in_executor(_worker, reindex)
        log.info("search backfill: %s", {k: {x: v[x] for x in ("documents", "indexed", "pruned",
                                                               "seconds")}
                                         for k, v in counts.items()})
        await asyncio.sleep(BACKFILL_EVERY_S)


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def search(source_type: str, query: str, *, allowed: str | None = None,
           params: Iterable[Any] = (), limit: int = 10) -> engine.SearchResult:
    conn = get_connection("scopes")
    try:
        return engine.search(conn, query, source_type=source_type, allowed=allowed,
                             params=params, limit=limit)
    finally:
        conn.close()
