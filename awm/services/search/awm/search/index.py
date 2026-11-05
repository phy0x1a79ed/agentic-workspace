"""The archive's index: documents in, and the ranked reads a query is built from.

A hit names its document by id, title and date; it quotes none of it.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Sequence

from awm.config import privacy
from awm.persistence import embeddings as engine

from awm.search import store

#: How many matching passages the word search weighs before it ranks their documents.
CANDIDATES = 1000
#: The journal's project-level channel of ``<p>``: an empty ``project`` and this ``scope``.
PROJECT_CHANNEL = "project:{}"


def _tag_needle(project: str) -> str:
    return store.tag_field([project])


def veil(hidden: Sequence[dict] | None) -> tuple[str, list[Any]]:
    """SQL that keeps only the documents no private mark in ``hidden`` covers, and its bound values.

    A mark covers a document by its project and scope, or by its project's channel,
    and also by a tag equal to the marked project's name, ignoring case.
    ``None`` stands for an unreadable marks file: it keeps only documents that name no project,
    scope or tag.
    """
    if hidden is None:
        return "(project = '' AND scope = '' AND tags = '')", []
    clauses: list[str] = []
    params: list[Any] = []
    for mark in hidden:
        project, scope = mark["project"], mark["scope"]
        if scope is None:
            clauses.append("project = ? OR (project = '' AND scope = ?)")
            params += [project, PROJECT_CHANNEL.format(project)]
        else:
            clauses.append("project = ? AND (scope = ? OR substr(scope, 1, ?) = ?)")
            params += [project, scope, len(scope) + 1, scope + "/"]
    for project in sorted({m["project"] for m in hidden}):
        clauses.append("instr(tags, ?) > 0")
        params.append(_tag_needle(project))
    if not clauses:
        return "", []
    return "NOT (" + " OR ".join(f"({c})" for c in clauses) + ")", params


def covered(hidden: Sequence[dict] | None, project: str, scope: str, tags: str = "") -> bool:
    """Whether :func:`veil` would drop a document with this project, scope and stored tags."""
    if hidden is None:
        return bool(project or scope or tags)
    name, named_scope = privacy.canonical(project, scope or None)
    for mark in hidden:
        if tags and _tag_needle(mark["project"]) in tags:
            return True
        if name != mark["project"]:
            continue
        marked = mark["scope"]
        if marked is None or (named_scope is not None
                              and (named_scope == marked or named_scope.startswith(marked + "/"))):
            return True
    return False


def apply_page(conn: sqlite3.Connection, source: str, raw: list[dict[str, Any]]) -> dict[str, int]:
    """Store one exported page and bring the engine in line with it.

    A document that has lost all its content is dropped rather than kept stale.
    """
    pairs = [(d, store.clean(source, d)) for d in raw if isinstance(d, dict)]
    rows = [row for _, row in pairs if row]
    emptied = [str(d["id"]) for d, row in pairs if row is None and d.get("id")]
    store.drop_documents(conn, source, emptied)
    changed = store.put_documents(conn, rows)
    counts = engine.reindex(conn, store.CORPUS, [store.engine_document(r) for r in rows])
    return {"documents": len(rows), "changed": changed, "skipped": len(raw) - len(rows),
            "indexed": counts["indexed"]}


def reindex(conn: sqlite3.Connection, *, force: bool = False) -> dict[str, Any]:
    """Re-embed what is stale (everything with ``force``) from the stored documents."""
    docs = [store.engine_document(dict(r)) for r in conn.execute("SELECT * FROM documents")]
    return engine.reindex(conn, store.CORPUS, docs, force=force, prune=True)


# -- hits ------------------------------------------------------------------

#: ``(SELECT key FROM documents WHERE …, params)``: the documents a read may return, from ``compile.allowed``.
Allowed = tuple[str, list[Any]]


def _hit(row: sqlite3.Row) -> dict[str, Any]:
    return {"id": row["doc_id"], "title": row["title"], "ts": row["ts"]}


def _rows(conn: sqlite3.Connection, keys: list[str]) -> dict[str, sqlite3.Row]:
    if not keys:
        return {}
    return {r["key"]: r for r in conn.execute(
        f"SELECT * FROM documents WHERE key IN ({','.join('?' * len(keys))})", keys)}


def words(conn: sqlite3.Connection, text: str, allowed: Allowed, limit: int) -> list[dict[str, Any]]:
    """Documents with a passage holding any word of ``text``, best BM25 passage first."""
    expr = engine.fts_query(text)
    if not expr:
        return []
    sql, params = allowed
    found = conn.execute(
        "SELECT p.key, bm25(passages_words) AS rank"
        " FROM passages_words JOIN passages p ON p.id = passages_words.rowid"
        f" WHERE passages_words MATCH ? AND p.key IN ({sql}) ORDER BY rank LIMIT ?",
        [expr, *params, CANDIDATES * 4]).fetchall()
    keys = list(dict.fromkeys(p["key"] for p in found))[:limit]
    rows = _rows(conn, keys)
    return [_hit(rows[k]) for k in keys if k in rows]


def related(conn: sqlite3.Connection, text: str, allowed: Allowed,
            limit: int) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """Documents nearest ``text`` in meaning and words together, and the engine's ``degraded`` block.

    The engine fuses cosine with BM25 over any of the words. Without a model it ranks by the words alone.
    """
    if not text.strip():
        return [], None
    sql, params = allowed
    result = engine.search(conn, text, source_type=store.CORPUS, allowed=sql, params=params, limit=limit)
    keys = [h["source_id"] for h in result.hits]
    rows = _rows(conn, keys)
    return [_hit(rows[k]) for k in keys if k in rows], result.degraded


def resolve(conn: sqlite3.Connection, entries: list[dict[str, Any]], allowed: Allowed,
            limit: int) -> list[dict[str, Any]]:
    """Documents for the kb entry index's matches, best first, at most ``limit``.

    An entry names a document and a span of its title or text. A document that is gone, or that ``allowed``
    excludes, is dropped: the index is rebuilt nightly, so it can name what the archive has since forgotten.
    """
    best: dict[str, float] = {}
    for e in sorted(entries, key=lambda e: -e["score"]):
        best.setdefault(store.key_of(e["source"], e["doc"]), e["score"])
    if not best:
        return []
    sql, params = allowed
    keys = list(best)
    rows = {r["key"]: r for r in conn.execute(
        f"SELECT * FROM documents WHERE key IN ({','.join('?' * len(keys))}) AND key IN ({sql})",
        [*keys, *params])}
    return [_hit(rows[k]) for k in keys if k in rows][:limit]
