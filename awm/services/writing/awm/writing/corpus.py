"""Application logic for the writing corpus service.

Ported from the self-improvement ``corpus`` CLI, refactored to operate on a
service DB connection and return JSON-serializable dicts (no printing). Split by
surface:

- **read** (MCP + CLI + HTTP): :func:`search`, :func:`get`, :func:`stats`,
  :func:`vocab`, :func:`feed`.
- **write / maintenance** (CLI + HTTP only — gated off the MCP surface by the
  manifest ``surfaces`` field): :func:`add`, :func:`retag`, :func:`remove`,
  :func:`link_dup`, :func:`unlink_dup`, :func:`import_corpus`, :func:`embed`,
  :func:`dedup`.

Every mutation keeps the samples row, FTS index, tags, and embedding consistent
in one transaction, and re-embeds when content changes.
"""

from __future__ import annotations

import difflib
import json
import logging
import sqlite3
from pathlib import Path
from typing import Any

from . import config, db, index

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Row shaping
# ---------------------------------------------------------------------------


def _meta(conn: sqlite3.Connection, sid: str, *, score: float | None = None,
          include_text: bool = False) -> dict[str, Any]:
    r = db.get_sample(conn, sid)
    if r is None:
        return {"id": sid, "missing": True}
    out: dict[str, Any] = {
        "id": r["id"],
        "name": r["name"],
        "type": r["type"],
        "grade": r["grade"],
        "wordcount": r["wordcount"],
        "created": r["created"],
        "note": r["note"],
        "tags": db.tags_str(conn, sid),
        "dup_of": r["dup_of"],
        "web_link": r["web_link"],
    }
    if score is not None:
        out["score"] = score
    if include_text:
        out["text"] = r["content"] or ""
    return out


# ---------------------------------------------------------------------------
# Filtering + search
# ---------------------------------------------------------------------------


def _filtered_ids(
    conn: sqlite3.Connection,
    *,
    type: str | None = None,
    tag: list[str] | None = None,
    after: str | None = None,
    min_words: int | None = None,
    grade: str | None = None,
    include_dups: bool = False,
) -> list[str]:
    where: list[str] = []
    params: list[Any] = []
    if not include_dups:
        where.append("s.dup_of IS NULL")
    if type:
        where.append("s.type = ?")
        params.append(type)
    if after:
        where.append("s.created >= ?")
        params.append(after)
    if min_words:
        where.append("s.wordcount >= ?")
        params.append(int(min_words))
    if grade:
        where.append("s.grade = ?")
        params.append(grade)
    for t in tag or []:
        facet, _, value = t.partition(":")
        where.append(
            "EXISTS (SELECT 1 FROM tags t WHERE t.sample_id=s.id AND t.facet=? AND t.value=?)"
        )
        params.extend([facet, value])
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    rows = conn.execute(
        f"SELECT s.id FROM samples s{clause} ORDER BY s.created", params
    ).fetchall()
    return [r["id"] for r in rows]


def _keyword_ids(conn: sqlite3.Connection, query: str) -> list[str]:
    rows = conn.execute(
        "SELECT sample_id FROM samples_fts WHERE samples_fts MATCH ? ORDER BY rank",
        (query,),
    ).fetchall()
    return [r["sample_id"] for r in rows]


def search(
    conn: sqlite3.Connection,
    *,
    query: str | None = None,
    semantic: str | None = None,
    k: int = 20,
    type: str | None = None,
    tag: list[str] | None = None,
    after: str | None = None,
    min_words: int | None = None,
    grade: str | None = None,
    include_dups: bool = False,
) -> dict[str, Any]:
    """Keyword / semantic / hybrid search with AND-ed metadata + tag filters.

    - ``query`` only → FTS5 keyword (BM25 rank).
    - ``semantic`` only → cosine kNN over the corpus embeddings.
    - both → hybrid: filtered keyword hits first, then a semantic top-up (>0.3).
    - neither → most-recent-first listing.
    Duplicates excluded unless ``include_dups``.

    If the semantic stack isn't installed on this node the semantic leg is
    dropped and the payload carries a ``degraded`` block: a semantic-only search
    falls back to keyword over the same string (and to a listing if that string
    isn't valid FTS syntax), so the caller gets real samples plus an explicit
    "could not rank by meaning" rather than a confident empty result.
    """
    allowed = _filtered_ids(
        conn, type=type, tag=tag, after=after, min_words=min_words,
        grade=grade, include_dups=include_dups,
    )
    allowed_set = set(allowed)

    ordered: list[tuple[str, float | None]] = []
    degraded: dict[str, Any] | None = None
    if query and semantic:
        kw = [i for i in _keyword_ids(conn, query) if i in allowed_set]
        ordered = [(i, None) for i in kw]
        seen = set(kw)
        try:
            hits = index.search_semantic(conn, semantic, limit=200)
        except index.EmbeddingsUnavailable:
            # The keyword leg already produced results; just say the top-up
            # didn't happen.
            degraded, hits = index.degraded_marker("writing", fallback="keyword"), []
        for h in hits:
            sid = h["source_id"]
            if sid in seen or sid not in allowed_set or h["score"] <= 0.3:
                continue
            ordered.append((sid, h["score"]))
            seen.add(sid)
    elif semantic:
        try:
            scored = {h["source_id"]: h["score"]
                      for h in index.search_semantic(conn, semantic, limit=300)}
            ranked = sorted(((i, scored[i]) for i in allowed if i in scored),
                            key=lambda t: t[1], reverse=True)
            ordered = [(i, s) for i, s in ranked]
        except index.EmbeddingsUnavailable:
            # Try the same string as keywords. Unlike notes, this path hands the
            # raw string to FTS5 with no sanitizer, so punctuation is a syntax
            # error — fall through to the listing rather than turning a missing
            # dependency into a query-syntax failure.
            try:
                ordered = [(i, None) for i in _keyword_ids(conn, semantic) if i in allowed_set]
                fallback = "keyword"
            except sqlite3.OperationalError:
                ordered, fallback = [(i, None) for i in allowed[::-1]], "listing"
            if not ordered:
                ordered, fallback = [(i, None) for i in allowed[::-1]], "listing"
            degraded = index.degraded_marker("writing", fallback=fallback)
    elif query:
        ordered = [(i, None) for i in _keyword_ids(conn, query) if i in allowed_set]
    else:
        ordered = [(i, None) for i in allowed[::-1]]  # most recent first

    ordered = ordered[: int(k)]
    out: dict[str, Any] = {
        "count": len(ordered),
        "results": [_meta(conn, sid, score=score) for sid, score in ordered],
    }
    if degraded:
        out["degraded"] = degraded
    return out


def get(conn: sqlite3.Connection, sample_id: str, *, include_text: bool = True) -> dict[str, Any]:
    r = db.get_sample(conn, sample_id)
    if r is None:
        raise ValueError(f"no such sample: {sample_id}")
    return _meta(conn, sample_id, include_text=include_text)


def stats(conn: sqlite3.Connection) -> dict[str, Any]:
    rows = db.all_samples(conn)
    keepers = [r for r in rows if not r["dup_of"]]
    dups = [r for r in rows if r["dup_of"]]
    emb = conn.execute(
        "SELECT COUNT(*) FROM embeddings WHERE source_type=?", (config.SOURCE_TYPE,)
    ).fetchone()[0]
    untagged = conn.execute(
        "SELECT COUNT(*) FROM samples s WHERE NOT EXISTS "
        "(SELECT 1 FROM tags t WHERE t.sample_id=s.id)"
    ).fetchone()[0]
    by_type: dict[str, int] = {}
    for r in keepers:
        by_type[r["type"] or "unsorted"] = by_type.get(r["type"] or "unsorted", 0) + 1
    graded = conn.execute(
        "SELECT grade, COUNT(*) AS n FROM samples WHERE dup_of IS NULL GROUP BY grade"
    ).fetchall()
    return {
        "samples": len(rows),
        "keepers": len(keepers),
        "dups": len(dups),
        "embedded": emb,
        "untagged": untagged,
        "by_type": by_type,
        "by_grade": {(g["grade"] or "untagged"): g["n"] for g in graded},
    }


def vocab() -> dict[str, Any]:
    """The closed tag vocabulary so callers know valid filter/tag values."""
    return {"vocab": config.VOCAB, "single_value_facets": sorted(config.SINGLE_VALUE_FACETS)}


def feed(
    conn: sqlite3.Connection,
    *,
    grade: str | None = None,
    tag: list[str] | None = None,
    type: str | None = None,
    after: str | None = None,
    min_words: int | None = None,
) -> dict[str, Any]:
    """Style-reference feed: full-text records for a filtered keeper set.

    Defaults to the ``grade:style-grade`` non-duplicate keepers — the
    voice-matching feed — unless the caller narrows with their own filters.
    """
    if not grade and not tag:
        grade = "style-grade"
    ids = _filtered_ids(conn, type=type, tag=tag, after=after,
                        min_words=min_words, grade=grade, include_dups=False)
    records = [_meta(conn, sid, include_text=True) for sid in ids]
    return {"count": len(records), "records": records}


# ---------------------------------------------------------------------------
# Mutation (write / maintenance)
# ---------------------------------------------------------------------------


def add(
    conn: sqlite3.Connection,
    *,
    text: str,
    name: str | None = None,
    type: str = "unsorted",
    tag: list[str] | None = None,
    note: str = "manually added",
    file: str | None = None,
) -> dict[str, Any]:
    """Add a new sample from inline text. Content is stored in-DB and embedded."""
    if not text or not text.strip():
        raise ValueError("text is required and must be non-empty")
    chash = db.content_hash(text)
    sid = chash[:16]  # stable content-derived id for manually-added samples
    row = {
        "id": sid,
        "name": name or f"sample_{sid}",
        "file": file,
        "type": type or "unsorted",
        "created": "",
        "modified": "",
        "wordcount": len(text.split()),
        "note": note,
        "web_link": None,
        "content": text,
        "content_hash": chash,
    }
    db.upsert_sample(conn, row)
    db.fts_replace(conn, sid, row["name"], note, text)
    for spec in tag or []:
        facet, _, value = spec.partition(":")
        db.set_tag(conn, sid, facet, value)
    deferred = False
    try:
        index.embed_sample(conn, sid, text)
    except index.EmbeddingsUnavailable:
        # The sample itself must land — it is the user's text. ``embedded_hash``
        # stays stale on purpose, which is exactly what ``embed`` looks for once
        # the stack is installed.
        log.warning("writing: embedding unavailable, %s added unindexed", sid)
        deferred = True
    else:
        conn.execute("UPDATE samples SET embedded_hash=? WHERE id=?", (chash, sid))
    conn.commit()
    out = get(conn, sid, include_text=False)
    if deferred:
        out["embedding_deferred"] = True
    return out


def retag(
    conn: sqlite3.Connection, sample_id: str, *,
    add: list[str] | None = None, rm: list[str] | None = None,
) -> dict[str, Any]:
    if db.get_sample(conn, sample_id) is None:
        raise ValueError(f"no such sample: {sample_id}")
    for spec in rm or []:
        facet, _, value = spec.partition(":")
        db.clear_tag(conn, sample_id, facet, value)
    for spec in add or []:
        facet, _, value = spec.partition(":")
        db.set_tag(conn, sample_id, facet, value)
    conn.commit()
    return get(conn, sample_id, include_text=False)


def remove(conn: sqlite3.Connection, sample_id: str) -> dict[str, Any]:
    if db.get_sample(conn, sample_id) is None:
        raise ValueError(f"no such sample: {sample_id}")
    index.drop_embedding(conn, sample_id)
    db.delete_sample(conn, sample_id)
    conn.commit()
    return {"removed": sample_id}


def _link_cluster(conn: sqlite3.Connection, keeper: str, dups: list[str]) -> int:
    for d in dups:
        conn.execute(
            "UPDATE samples SET dup_of=?, dup_cluster=? WHERE id=?", (keeper, keeper, d)
        )
    conn.execute("UPDATE samples SET dup_cluster=? WHERE id=?", (keeper, keeper))
    return len(dups)


def link_dup(conn: sqlite3.Connection, keeper: str, dups: list[str]) -> dict[str, Any]:
    if db.get_sample(conn, keeper) is None:
        raise ValueError(f"no such keeper: {keeper}")
    n = _link_cluster(conn, keeper, dups)
    conn.commit()
    return {"keeper": keeper, "linked": n}


def unlink_dup(conn: sqlite3.Connection, sample_id: str) -> dict[str, Any]:
    conn.execute(
        "UPDATE samples SET dup_of=NULL, dup_cluster=NULL WHERE id=?", (sample_id,)
    )
    conn.commit()
    return {"unlinked": sample_id}


# ---- dedup ---------------------------------------------------------------

DEDUP_COS = 0.85       # embedding-cosine gate to even consider a pair
DEDUP_RATIO_HI = 0.90  # difflib ratio above which we auto-cluster
DEDUP_RATIO_LO = 0.70  # below which we treat as distinct


def _ratio(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, db.normalize(a), db.normalize(b)).ratio()


def _candidates(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    texts: dict[str, str] = {}

    def text_of(sid: str) -> str:
        if sid not in texts:
            r = db.get_sample(conn, sid)
            texts[sid] = (r["content"] or "") if r else ""
        return texts[sid]

    out = []
    for ida, idb, cos in index.pairwise_cosine(conn):
        if cos < DEDUP_COS:
            break  # list is sorted desc
        ratio = _ratio(text_of(ida), text_of(idb))
        out.append({"a": ida, "b": idb, "cos": cos, "ratio": round(ratio, 4)})
    return out


def _union_find(pairs: list[tuple[str, str]]) -> dict[str, str]:
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a, b in pairs:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra
    return {x: find(x) for x in parent}


def _keeper(conn: sqlite3.Connection, ids: list[str]) -> str:
    rows = [db.get_sample(conn, i) for i in ids]
    rows.sort(key=lambda r: (r["wordcount"] or 0, r["modified"] or ""), reverse=True)
    return rows[0]["id"]


def dedup(conn: sqlite3.Connection, *, apply: bool = False) -> dict[str, Any]:
    """Content-based duplicate detection (embedding-gated, difflib-confirmed)."""
    cands = _candidates(conn)
    confident = [(c["a"], c["b"]) for c in cands if c["ratio"] >= DEDUP_RATIO_HI]
    borderline = [c for c in cands if DEDUP_RATIO_LO <= c["ratio"] < DEDUP_RATIO_HI]

    roots = _union_find(confident)
    clusters: dict[str, list[str]] = {}
    for node, root in roots.items():
        clusters.setdefault(root, []).append(node)

    applied = 0
    cluster_report = []
    for members in clusters.values():
        keeper = _keeper(conn, members)
        cluster_report.append({"keeper": keeper,
                               "dups": [m for m in members if m != keeper]})
        if apply:
            applied += _link_cluster(conn, keeper, [m for m in members if m != keeper])
    if apply:
        conn.commit()
    return {
        "clusters": cluster_report,
        "borderline": borderline,
        "applied": applied,
        "dry_run": not apply,
    }


def embed(conn: sqlite3.Connection, *, force: bool = False) -> dict[str, Any]:
    """Embed new/changed samples (or all, with ``force``). Loads the model once.

    Raises :class:`index.EmbeddingsUnavailable` when the stack is missing rather
    than reporting ``{"embedded": 0}`` — a backfill that silently does nothing
    is worse than one that fails, because the caller stops looking.
    """
    p = index.probe()
    if not p["available"]:
        raise index.EmbeddingsUnavailable(
            f"cannot embed: missing {', '.join(p['missing'])} — "
            "run awm/services/writing/install.sh on this node"
        )
    rows = db.all_samples(conn)
    todo = [r for r in rows if force or r["embedded_hash"] != r["content_hash"]]
    for r in todo:
        text = r["content"] or ""
        index.embed_sample(conn, r["id"], text)
        conn.execute(
            "UPDATE samples SET embedded_hash=? WHERE id=?", (r["content_hash"], r["id"])
        )
        conn.commit()
    return {"embedded": len(todo)}


def import_corpus(conn: sqlite3.Connection, *, corpus_dir: str | None = None) -> dict[str, Any]:
    """Bulk-ingest a manifest.json + text-file corpus into the service DB.

    Reads ``<corpus_dir>/manifest.json`` (``kept`` entries), stores each sample's
    text in-DB, refreshes FTS, and embeds new/changed samples. Idempotent on the
    normalized content hash. ``corpus_dir`` defaults to the historical
    self-improvement corpus.
    """
    root = Path(corpus_dir) if corpus_dir else config.default_corpus_dir()
    manifest = json.loads((root / "manifest.json").read_text())
    kept = manifest.get("kept", [])
    changed = 0
    for entry in kept:
        sid = entry["id"]
        text = (root / entry["file"]).read_text(encoding="utf-8", errors="replace")
        chash = db.content_hash(text)
        prev = db.get_sample(conn, sid)
        row = {
            "id": sid,
            "name": entry["name"],
            "file": entry["file"],
            "type": entry.get("type"),
            "created": (entry.get("createdTime") or "")[:10],
            "modified": (entry.get("modifiedTime") or "")[:10],
            "wordcount": entry.get("wordcount"),
            "note": entry.get("reason"),
            "web_link": entry.get("webViewLink"),
            "content": text,
            "content_hash": chash,
        }
        db.upsert_sample(conn, row)
        db.fts_replace(conn, sid, entry["name"], entry.get("reason", ""), text)
        if prev is None or prev["content_hash"] != chash:
            changed += 1
    conn.commit()
    # The text is the import; embedding is a follow-up. A stackless node still
    # ingests the corpus and leaves every row stale for a later ``embed``.
    try:
        return {"ingested": len(kept), "changed": changed,
                "embedded": embed(conn)["embedded"]}
    except index.EmbeddingsUnavailable:
        log.warning("writing: embedding unavailable, %d samples imported unindexed", len(kept))
        return {"ingested": len(kept), "changed": changed,
                "embedded": 0, "embedding_deferred": True}
