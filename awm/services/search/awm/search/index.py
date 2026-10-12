"""The archive's index: documents into the retrieval engine, and the filtered query over them."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Any

from awm.persistence import embeddings as engine

from awm.search import store

SNIPPET_CHARS = 300


@dataclass
class Filters:
    """What narrows a query. ``after`` is inclusive and ``before`` exclusive, both normalised."""

    sources: list[str] = field(default_factory=list)
    project: str = ""
    scope: str = ""
    kind: str = ""
    after: str = ""
    before: str = ""

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
