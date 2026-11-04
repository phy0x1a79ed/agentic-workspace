"""The archive's index: documents in, and the filtered query out in sections.

``exact`` lists the documents that hold the query literally, then those with a
passage holding all its words. ``related`` ranks the rest by meaning and words.
A hit names its document; it quotes none of it.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Sequence

from awm.config import privacy
from awm.persistence import embeddings as engine

from awm.search import store

#: How many matching documents a literal or keyword query weighs before it ranks them.
CANDIDATES = 1000
#: The shortest literal the trigram index can find; a shorter one scans.
TRIGRAM = 3
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


@dataclass
class Filters:
    """What narrows a query. ``after`` is inclusive and ``before`` exclusive, both normalised.

    ``hidden`` holds the private marks the caller may not see past, as :func:`veil` reads them.
    """

    sources: list[str] = field(default_factory=list)
    project: str = ""
    scope: str = ""
    #: ``[(negated, tag), ...]``, every term held against the ``tags`` column.
    tags: list[tuple[bool, str]] = field(default_factory=list)
    after: str = ""
    before: str = ""
    hidden: Sequence[dict] | None = ()

    def allowed(self) -> tuple[str, list[Any]]:
        """A ``SELECT key`` over the permitted documents, and its bound values."""
        clauses = [f"source IN ({','.join('?' * len(self.sources))})"]
        params: list[Any] = list(self.sources)
        for column, value in (("project", self.project), ("scope", self.scope)):
            if value:
                clauses.append(f"{column} = ?")
                params.append(value)
        for negated, tag in self.tags:
            clauses.append(f"{'NOT ' if negated else ''}instr(tags, ?) > 0")
            params.append(store.tag_field([tag]))
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


def _hit(row: sqlite3.Row, match: str, score: float) -> dict[str, Any]:
    hit = {"source": row["source"], "id": row["doc_id"], "title": row["title"], "ts": row["ts"],
           "score": round(score, 4), "match": match}
    hit.update({k: row[k] for k in ("project", "scope", "kind", "url") if row[k]})
    return hit


def exact_literal(conn: sqlite3.Connection, query: str, filters: Filters, limit: int) -> list[dict[str, Any]]:
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
        body = len(pattern.findall(row["text"]))
        head = len(pattern.findall(row["title"]))
        if not (body or head):
            continue
        cased = needle in row["text"] or needle in row["title"]
        ranked.append(((cased, bool(head), body + head, row["ts"]), row))
    ranked.sort(key=lambda r: r[0], reverse=True)
    return [_hit(row, "literal", float(rank[2])) for rank, row in ranked[:limit]]


def exact_keyword(conn: sqlite3.Connection, query: str, filters: Filters, limit: int,
                  skip: set[str] = frozenset(), every: bool = True) -> list[dict[str, Any]]:
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
        "SELECT p.key, bm25(passages_words) AS rank"
        " FROM passages_words JOIN passages p ON p.id = passages_words.rowid"
        f" WHERE passages_words MATCH ? AND p.key IN ({allowed}) ORDER BY rank LIMIT ?",
        [expr, *params, CANDIDATES * 4]).fetchall()
    by_key: dict[str, float] = {}
    for p in found:
        if p["key"] not in skip and p["key"] not in by_key and len(by_key) < limit:
            by_key[p["key"]] = p["rank"]
    if not by_key:
        return []
    keys = list(by_key)
    rows = {r["key"]: r for r in conn.execute(
        f"SELECT * FROM documents WHERE key IN ({','.join('?' * len(keys))})", keys)}
    return [_hit(rows[key], "keyword", -rank) for key, rank in by_key.items() if key in rows]


def related(conn: sqlite3.Connection, query: str, filters: Filters,
            limit: int) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """Documents nearest the query in meaning and words together.

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
    return [_hit(rows[h["source_id"]], match, h["score"]) for h in result.hits if h["source_id"] in rows], \
        result.degraded


def exact(conn: sqlite3.Connection, query: str, filters: Filters, limit: int) -> list[dict[str, Any]]:
    """The ``exact`` section: literal matches, then documents with a passage holding every word."""
    literal_hits = exact_literal(conn, query, filters, limit)
    seen = {store.key_of(h["source"], h["id"]) for h in literal_hits}
    return literal_hits + exact_keyword(conn, query, filters, limit - len(literal_hits), seen)


def words(conn: sqlite3.Connection, query: str, filters: Filters, limit: int) -> list[dict[str, Any]]:
    """Documents with a passage holding any word of the query, best BM25 passage first: the word half of
    ``related`` for the sources whose meaning half comes from kb."""
    return exact_keyword(conn, query, filters, limit, every=False)


def search(conn: sqlite3.Connection, query: str, filters: Filters,
           limit: int) -> tuple[dict[str, list[dict[str, Any]]], dict[str, Any] | None]:
    """The ``exact`` and ``related`` sections over the selected sources, plus the engine's ``degraded`` block.

    ``related`` runs twice as deep as ``limit``, because the merge drops from it
    every document ``exact`` already holds.
    """
    near, degraded = related(conn, query, filters, limit * 2)
    return {"exact": exact(conn, query, filters, limit), "related": near}, degraded


def resolve(conn: sqlite3.Connection, entries: list[dict[str, Any]], filters: Filters,
            limit: int) -> list[dict[str, Any]]:
    """Documents for the kb entry index's matches, best first; at most ``limit`` per source.

    An entry names a document and a span of its title or text. A document that is gone, or that the filters
    exclude, is dropped: the index is rebuilt nightly, so it can name what the archive has since forgotten.
    """
    best: dict[str, float] = {}
    for e in sorted(entries, key=lambda e: -e["score"]):
        best.setdefault(store.key_of(e["source"], e["doc"]), e["score"])
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
        taken[row["source"]] = taken.get(row["source"], 0) + 1
        hits.append(_hit(row, "meaning", best[key]))
    return hits


#: The i-th seed of a walk weighs DECAY**i, so the first hit counts most.
DECAY = 0.9


def walk(conn: sqlite3.Connection, source: str, seeds: list[str], filters: Filters,
         limit: int) -> list[dict[str, Any]]:
    """The documents of ``source`` one ``reply_to`` link from ``seeds``, either way: those a seed replies to
    and those replying to a seed. A document scores the summed weight of the seeds it links, ties newest first.
    """
    if not seeds or not filters.sources:
        return []
    weight: dict[str, float] = {}
    for i, seed in enumerate(dict.fromkeys(seeds)):
        weight.setdefault(seed, DECAY ** i)
    allowed, params = filters.allowed()
    marks = ",".join("?" * len(weight))
    held = conn.execute(f"SELECT doc_id, reply_to FROM documents WHERE source=? AND doc_id IN ({marks})",
                        [source, *weight]).fetchall()
    score: dict[str, float] = {}
    for row in held:
        for target in store.id_list(row["reply_to"]):
            score[target] = score.get(target, 0.0) + weight[row["doc_id"]]
    for seed, w in weight.items():
        for row in conn.execute("SELECT doc_id FROM documents WHERE source=? AND instr(reply_to, ?) > 0",
                                (source, store.id_field([seed]))):
            score[row["doc_id"]] = score.get(row["doc_id"], 0.0) + w
    for seed in weight:
        score.pop(seed, None)
    if not score:
        return []
    keys = [store.key_of(source, d) for d in score]
    rows = conn.execute(f"SELECT * FROM documents WHERE key IN ({','.join('?' * len(keys))})"
                        f" AND key IN ({allowed})", [*keys, *params]).fetchall()
    rows.sort(key=lambda r: r["ts"], reverse=True)
    rows.sort(key=lambda r: -score[r["doc_id"]])
    return [_hit(r, "reply_to", score[r["doc_id"]]) for r in rows[:limit]]
