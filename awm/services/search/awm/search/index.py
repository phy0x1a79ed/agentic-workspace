"""The archive's index: documents in, and the filtered query out in sections.

``exact`` lists the documents that hold the query literally, then those with a
passage holding all its words. ``related`` ranks the rest by meaning and words.
Every hit quotes its passages with their spans in the document.
"""

from __future__ import annotations

import bisect
import re
import sqlite3
from dataclasses import dataclass, field
from typing import Any

from awm.persistence import embeddings as engine

from awm.search import store

#: How many matching documents a literal or keyword query weighs before it ranks them.
CANDIDATES = 1000
#: The shortest literal the trigram index can find; a shorter one scans.
TRIGRAM = 3


@dataclass
class Filters:
    """What narrows a query. ``after`` is inclusive and ``before`` exclusive, both normalised."""

    sources: list[str] = field(default_factory=list)
    project: str = ""
    scope: str = ""
    kinds: list[str] = field(default_factory=list)
    after: str = ""
    before: str = ""

    def allowed(self) -> tuple[str, list[Any]]:
        """A ``SELECT key`` over the permitted documents, and its bound values."""
        clauses = [f"source IN ({','.join('?' * len(self.sources))})"]
        params: list[Any] = list(self.sources)
        for column, value in (("project", self.project), ("scope", self.scope)):
            if value:
                clauses.append(f"{column} = ?")
                params.append(value)
        if self.kinds:
            clauses.append(f"kind IN ({','.join('?' * len(self.kinds))})")
            params += self.kinds
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
    changed = store.put_documents(conn, rows)
    counts = engine.reindex(conn, store.CORPUS, [store.engine_document(r) for r in rows])
    return {"documents": len(rows), "changed": changed, "skipped": len(raw) - len(rows),
            "indexed": counts["indexed"]}


def reindex(conn: sqlite3.Connection, *, force: bool = False) -> dict[str, Any]:
    """Re-embed what is stale (everything with ``force``) from the stored documents."""
    docs = [store.engine_document(dict(r)) for r in conn.execute("SELECT * FROM documents")]
    return engine.reindex(conn, store.CORPUS, docs, force=force, prune=True)


# -- hits ------------------------------------------------------------------


def literal(query: str) -> str:
    """The text an exact search looks for: the query, without one pair of surrounding quotes."""
    text = query.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'`":
        text = text[1:-1]
    return text


def _hit(row: sqlite3.Row, match: str, score: float, passages: list[dict[str, Any]]) -> dict[str, Any]:
    hit = {"source": row["source"], "id": row["doc_id"], "title": row["title"], "ts": row["ts"],
           "score": round(score, 4), "match": match, "passages": passages}
    hit.update({k: row[k] for k in ("project", "scope", "kind", "url") if row[k]})
    return hit


def _passages_of(conn: sqlite3.Connection, key: str) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM passages WHERE key=? AND field='text' ORDER BY start", (key,)).fetchall()


def _quote(field_name: str, start: int, end: int, section: str, text: str,
           marks: list[list[int]] | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {"field": field_name, "start": start, "end": end, "text": text[start:end]}
    if section:
        out["section"] = section
    if marks:
        out["marks"] = marks
    return out


def _literal_quotes(conn: sqlite3.Connection, row: sqlite3.Row, spans: list[tuple[int, int]],
                    in_title: list[tuple[int, int]], per_hit: int) -> list[dict[str, Any]]:
    """The passages that hold the matches, each widened to cover a match that runs past its end."""
    quotes = []
    if in_title:
        quotes.append(_quote("title", 0, len(row["title"]), "", row["title"], [list(s) for s in in_title]))
    cut = _passages_of(conn, row["key"])
    starts = [p["start"] for p in cut]
    picked: dict[int, list[tuple[int, int]]] = {}
    for s, e in spans:
        i = max(0, bisect.bisect_right(starts, s) - 1)
        if i in picked or len(picked) < per_hit:
            picked.setdefault(i, []).append((s, e))
    for i, marks in sorted(picked.items()):
        p = cut[i] if cut else None
        a = min(p["start"], marks[0][0]) if p else marks[0][0]
        b = max(p["stop"], marks[-1][1]) if p else marks[-1][1]
        quotes.append(_quote("text", a, b, p["section"] if p else "", row["text"], [list(m) for m in marks]))
    return quotes[:per_hit]


def exact_literal(conn: sqlite3.Connection, query: str, filters: Filters, limit: int,
                  per_hit: int) -> list[dict[str, Any]]:
    """Documents holding ``query`` as a substring, case-insensitively, as grep would find them.

    A case-sensitive match ranks above a case-folded one, a match in the title
    above one in the body, more matches above fewer, and then newer first.
    """
    needle = literal(query)
    if not needle.strip() or not filters.sources:
        return []
    allowed, params = filters.allowed()
    if len(needle) >= TRIGRAM:
        rows = conn.execute(
            "SELECT d.* FROM documents_exact JOIN documents d ON d.rid = documents_exact.rowid"
            f" WHERE documents_exact MATCH ? AND d.key IN ({allowed}) ORDER BY d.ts DESC LIMIT ?",
            ['"' + needle.replace('"', '""') + '"', *params, CANDIDATES]).fetchall()
    else:
        rows = conn.execute(
            f"SELECT * FROM documents WHERE key IN ({allowed})"
            " AND (instr(lower(title), lower(?)) > 0 OR instr(lower(text), lower(?)) > 0)"
            " ORDER BY ts DESC LIMIT ?", [*params, needle, needle, CANDIDATES]).fetchall()
    pattern = re.compile(re.escape(needle), re.IGNORECASE)
    ranked = []
    for row in rows:
        body = [m.span() for m in pattern.finditer(row["text"])]
        head = [m.span() for m in pattern.finditer(row["title"])]
        if not (body or head):
            continue
        cased = needle in row["text"] or needle in row["title"]
        ranked.append(((cased, bool(head), len(body) + len(head), row["ts"]), row, body, head))
    ranked.sort(key=lambda r: r[0], reverse=True)
    return [_hit(row, "literal", float(rank[2]), _literal_quotes(conn, row, body, head, per_hit))
            for rank, row, body, head in ranked[:limit]]


def exact_keyword(conn: sqlite3.Connection, query: str, filters: Filters, limit: int,
                  per_hit: int, skip: set[str] = frozenset(), every: bool = True) -> list[dict[str, Any]]:
    """Documents with a passage holding every word of the query, best BM25 passage first; ``skip`` names keys to
    leave out. A document holding only some of the words is left to ``related``, which ranks it by meaning too.
    ``every=False`` asks for any of the words instead."""
    expr = engine.fts_query(query)
    if every:
        expr = expr.replace(" OR ", " AND ")
    if not expr or not filters.sources:
        return []
    allowed, params = filters.allowed()
    found = conn.execute(
        "SELECT p.key, p.field, p.start, p.stop, p.section, bm25(passages_words) AS rank"
        " FROM passages_words JOIN passages p ON p.id = passages_words.rowid"
        f" WHERE passages_words MATCH ? AND p.key IN ({allowed}) ORDER BY rank LIMIT ?",
        [expr, *params, CANDIDATES * 4]).fetchall()
    by_key: dict[str, list[sqlite3.Row]] = {}
    for p in found:
        if p["key"] not in skip and (p["key"] in by_key or len(by_key) < limit):
            by_key.setdefault(p["key"], []).append(p)
    if not by_key:
        return []
    keys = list(by_key)
    rows = {r["key"]: r for r in conn.execute(
        f"SELECT * FROM documents WHERE key IN ({','.join('?' * len(keys))})", keys)}
    hits = []
    for key in keys:
        row = rows.get(key)
        if row is None:
            continue
        quotes = [_quote(p["field"], p["start"], p["stop"], p["section"],
                         row["title"] if p["field"] == "title" else row["text"]) for p in by_key[key][:per_hit]]
        hits.append(_hit(row, "keyword", -by_key[key][0]["rank"], quotes))
    return hits


def _related_quote(conn: sqlite3.Connection, row: sqlite3.Row, chunk_text: str) -> list[dict[str, Any]]:
    """The passage where the engine's best chunk begins; the title when the chunk is not in the body."""
    lead = chunk_text.split("\n", 1)[0].strip()[:120]
    at = row["text"].find(lead) if lead else -1
    if at < 0:
        return [_quote("title", 0, len(row["title"]), "", row["title"])] if row["title"] else []
    cut = _passages_of(conn, row["key"])
    i = max(0, bisect.bisect_right([p["start"] for p in cut], at) - 1)
    if not cut:
        return []
    return [_quote("text", cut[i]["start"], cut[i]["stop"], cut[i]["section"], row["text"])]


def related(conn: sqlite3.Connection, query: str, filters: Filters,
            limit: int) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """Documents nearest the query in meaning and words together, each with the passage that matched best.

    The engine fuses cosine with BM25 over any of the query's words. Without a
    model it ranks by the words alone and says so in the ``degraded`` block.
    """
    if not filters.sources or not query.strip():
        return [], None
    allowed, params = filters.allowed()
    result = engine.search(conn, query, source_type=store.CORPUS, allowed=allowed, params=params, limit=limit)
    match = "keyword" if result.degraded else "meaning"
    keys = [h["source_id"] for h in result.hits]
    rows = {r["key"]: r for r in conn.execute(
        f"SELECT * FROM documents WHERE key IN ({','.join('?' * len(keys))})", keys)} if keys else {}
    return [_hit(rows[h["source_id"]], match, h["score"], _related_quote(conn, rows[h["source_id"]], h["snippet"]))
            for h in result.hits if h["source_id"] in rows], result.degraded


def exact(conn: sqlite3.Connection, query: str, filters: Filters, limit: int, per_hit: int) -> list[dict[str, Any]]:
    """The ``exact`` section: literal matches, then documents with a passage holding every word."""
    literal_hits = exact_literal(conn, query, filters, limit, per_hit)
    seen = {store.key_of(h["source"], h["id"]) for h in literal_hits}
    return literal_hits + exact_keyword(conn, query, filters, limit - len(literal_hits), per_hit, seen)


def words(conn: sqlite3.Connection, query: str, filters: Filters, limit: int, per_hit: int) -> list[dict[str, Any]]:
    """Documents with a passage holding any word of the query, best BM25 passage first: the word half of
    ``related`` for the sources whose meaning half comes from kb."""
    return exact_keyword(conn, query, filters, limit, per_hit, every=False)


def search(conn: sqlite3.Connection, query: str, filters: Filters, limit: int,
           per_hit: int) -> tuple[dict[str, list[dict[str, Any]]], dict[str, Any] | None]:
    """The ``exact`` and ``related`` sections over the selected sources, plus the engine's ``degraded`` block.

    ``related`` runs twice as deep as ``limit``, because the merge drops from it
    every document ``exact`` already holds.
    """
    near, degraded = related(conn, query, filters, limit * 2)
    return {"exact": exact(conn, query, filters, limit, per_hit), "related": near}, degraded


def resolve(conn: sqlite3.Connection, entries: list[dict[str, Any]], filters: Filters, limit: int,
            per_hit: int) -> list[dict[str, Any]]:
    """Documents for the kb entry index's matches, best first, each quoting the spans its entries cover; at most
    ``limit`` per source.

    An entry names a document and a span of its title or text. A document that is gone, or that the filters
    exclude, is dropped: the index is rebuilt nightly, so it can name what the archive has since forgotten.
    """
    best: dict[str, list[dict[str, Any]]] = {}
    for e in sorted(entries, key=lambda e: -e["score"]):
        best.setdefault(store.key_of(e["source"], e["doc"]), []).append(e)
    if not best or not filters.sources:
        return []
    allowed, params = filters.allowed()
    keys = list(best)
    rows = {r["key"]: r for r in conn.execute(
        f"SELECT * FROM documents WHERE key IN ({','.join('?' * len(keys))}) AND key IN ({allowed})",
        [*keys, *params])}
    hits, taken = [], {}
    for key in keys:
        row = rows.get(key)
        if row is None or taken.get(row["source"], 0) >= limit:
            continue
        quotes = []
        for e in best[key]:
            body = row["title"] if e["part"] == "title" else row["text"]
            if e["part"] not in ("title", "text") or e["start"] >= len(body):
                continue
            cut = conn.execute("SELECT section FROM passages WHERE key = ? AND field = ? AND start <= ? AND stop > ?"
                               " ORDER BY start DESC LIMIT 1", (key, e["part"], e["start"], e["start"])).fetchone()
            quote = _quote(e["part"], e["start"], min(e["end"], len(body)), cut["section"] if cut else "", body)
            if all((quote["field"], quote["start"]) != (q["field"], q["start"]) for q in quotes):
                quotes.append(quote)
            if len(quotes) == per_hit:
                break
        if taken.get(row["source"], 0) < limit:
            taken[row["source"]] = taken.get(row["source"], 0) + 1
            hits.append(_hit(row, "meaning", best[key][0]["score"], quotes))
    return hits
