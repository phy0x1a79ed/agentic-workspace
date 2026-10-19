"""The archive's index: documents into the retrieval engine, and the filtered query over them."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Any, Sequence

from awm.config import privacy
from awm.persistence import embeddings as engine

from awm.search import store

SNIPPET_CHARS = 300
#: The journal's spellings of a project ``<p>`` besides its bare name, in the ``project`` column ...
PROJECT_SPELLINGS = ("task:{}",)
#: ... and, with an empty ``project``, in the ``scope`` column. ``privacy.canonical`` reads all of them.
CHANNEL_SPELLINGS = ("user:proj:{}", "project:{}")


def _tag_needle(project: str) -> str:
    return store.tag_field([project])


def veil(hidden: Sequence[dict] | None) -> tuple[str, list[Any]]:
    """SQL that keeps only the documents no private mark in ``hidden`` covers, and its bound values.

    A mark covers a document by its project and scope, in any spelling ``privacy.canonical`` reads,
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
        named = [project] + [s.format(project) for s in PROJECT_SPELLINGS]
        in_named = f"project IN ({','.join('?' * len(named))})"
        if scope is None:
            channels = [s.format(project) for s in CHANNEL_SPELLINGS]
            clauses.append(f"{in_named} OR (project = '' AND scope IN ({','.join('?' * len(channels))}))")
            params += named + channels
        else:
            clauses.append(f"{in_named} AND (scope = ? OR substr(scope, 1, ?) = ?)")
            params += named + [scope, len(scope) + 1, scope + "/"]
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


@dataclass
class Filters:
    """What narrows a query. ``after`` is inclusive and ``before`` exclusive, both normalised.

    ``hidden`` holds the private marks the caller may not see past, as :func:`veil` reads them.
    """

    sources: list[str] = field(default_factory=list)
    project: str = ""
    scope: str = ""
    kind: str = ""
    after: str = ""
    before: str = ""
    hidden: Sequence[dict] | None = ()

    def allowed(self) -> tuple[str, list[Any]]:
        """The SQL the engine applies before scoring, and its bound values."""
        clauses = [f"source IN ({','.join('?' * len(self.sources))})"]
        params: list[Any] = list(self.sources)
        for column, value in (("project", self.project), ("scope", self.scope), ("kind", self.kind)):
            if value:
                clauses.append(f"{column} = ?")
                params.append(value)
        if self.after:
            clauses.append("ts >= ?")
            params.append(self.after)
        if self.before:
            clauses.append("ts < ? AND ts != ''")
            params.append(self.before)
        kept, kept_params = veil(self.hidden)
        if kept:
            clauses.append(kept)
            params += kept_params
        return "SELECT key FROM documents WHERE " + " AND ".join(clauses), params


def apply_page(conn: sqlite3.Connection, source: str, raw: list[dict[str, Any]]) -> dict[str, int]:
    """Store one exported page and bring the engine in line with it.

    A document that has lost all its content is dropped rather than kept stale.
    """
    pairs = [(d, store.clean(source, d)) for d in raw if isinstance(d, dict)]
    rows = [row for _, row in pairs if row]
    emptied = [str(d["id"]) for d, row in pairs if row is None and d.get("id")]
    store.drop_documents(conn, source, emptied)
    store.put_documents(conn, rows)
    counts = engine.reindex(conn, store.CORPUS, [store.engine_document(r) for r in rows])
    return {"documents": len(rows), "skipped": len(raw) - len(rows), "indexed": counts["indexed"]}


def reindex(conn: sqlite3.Connection, *, force: bool = False) -> dict[str, Any]:
    """Re-embed what is stale (everything with ``force``) from the stored documents."""
    docs = [store.engine_document(dict(r)) for r in conn.execute("SELECT * FROM documents")]
    return engine.reindex(conn, store.CORPUS, docs, force=force, prune=True)


def search(conn: sqlite3.Connection, query: str, filters: Filters,
           limit: int) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """Ranked hits over the selected sources, plus the engine's ``degraded`` block if any."""
    if not filters.sources:
        return [], None
    allowed, params = filters.allowed()
    found = engine.search(conn, query, source_type=store.CORPUS, allowed=allowed, params=params,
                          limit=limit)
    keys = [h["source_id"] for h in found.hits]
    rows = {r["key"]: r for r in conn.execute(
        f"SELECT * FROM documents WHERE key IN ({','.join('?' * len(keys))})", keys)} if keys else {}
    hits = []
    for h in found.hits:
        row = rows.get(h["source_id"])
        if row is None:
            continue
        hit = {"source": row["source"], "id": row["doc_id"], "title": row["title"],
               "snippet": " ".join(h["snippet"].split())[:SNIPPET_CHARS], "ts": row["ts"],
               "score": h["score"]}
        hit.update({k: row[k] for k in ("project", "scope", "kind", "url") if row[k]})
        hits.append(hit)
    return hits, found.degraded
