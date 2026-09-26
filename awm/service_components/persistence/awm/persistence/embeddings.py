"""Retrieval engine: chunk, embed and search documents in a service's own SQLite DB.

Every service that searches owns an ``embeddings`` table (one row per chunk)
and an ``embeddings_fts`` table in its own DB, and reaches them only through
this module: :func:`index_document` / :func:`reindex` to write,
:func:`search` to read. The pipeline was chosen by measurement against a
labelled query set (see ``awm/services/scopes/scripts/search_eval.py``):

- **Full-length chunks.** A document is split on headings, paragraphs and list
  starts into chunks of at most :data:`CHUNK_TOKENS` model tokens, so no part
  of a long journal falls outside the index.
- **Headers.** Each chunk is embedded and keyword-indexed with a deterministic
  header (title, context, date, section), so a chunk deep in a document still
  carries what the document is about.
- **MaxP.** A document scores as its best-matching chunk.
- **Hybrid.** FTS5 BM25 over the same chunks, fused with the dense score by a
  convex combination after min-max normalising each leg over its top
  :data:`FUSION_DEPTH`.
- **Filter first.** The caller's filter is a SQL subquery over its own tables,
  applied before scoring, so a selective filter never empties the result.

``sentence-transformers`` and ``numpy`` load lazily. Without them, indexing
still stores chunks for keyword search, and :func:`search` answers from BM25
with a ``degraded`` block rather than an empty or silently partial list.
"""

from __future__ import annotations

import hashlib
import importlib.util
import os
import re
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Protocol

_SEARCH_MODULES = ("sentence_transformers", "numpy")
_INSTALL_HINT = (
    "the awm-persistence[search] extra is not installed in this environment — "
    "run awm/services/<service>/install.sh on this node to add it"
)

MODEL_NAME = "all-MiniLM-L6-v2"
QUERY_PREFIX = ""
DOCUMENT_PREFIX = ""
# Every vector written before the chunked store was made by this model.
_LEGACY_MODEL = "all-MiniLM-L6-v2"

CHUNK_TOKENS = 128
ALPHA = 0.4
FUSION_DEPTH = 100
# Part of every content hash: bump it when chunking or headers change, and
# the next reindex rewrites every document.
CHUNKER_VERSION = 1


class EmbeddingsUnavailable(RuntimeError):
    """The semantic-search stack is not installed here.

    Distinct from "the search returned nothing": a caller that catches this
    knows its *capability* is missing, not that its query failed, and must say
    so rather than reporting an empty result set.
    """


def probe() -> dict[str, Any]:
    """Report whether the search stack is importable, without importing it."""
    missing = [m for m in _SEARCH_MODULES if importlib.util.find_spec(m) is None]
    return {"available": not missing, "missing": missing}


def degraded_marker(service: str, *, fallback: str) -> dict[str, Any]:
    """The block a read attaches to its payload when it could not search.

    A read that loses semantic ranking must not come back as a bare
    ``count: 0`` — that reads as a confident "no such thing" when the truth is
    "this node cannot answer that question". Callers attach this under a
    ``degraded`` key (absent when healthy, so existing consumers are unaffected)
    and still return whatever their keyword/fuzzy leg found. ``fallback`` names
    what actually produced the results — ``keyword``, ``fuzzy``, ``listing``, or
    ``none``.
    """
    return {
        "semantic": "unavailable",
        "missing": probe()["missing"],
        "fallback": fallback,
        "fix": f"run awm/services/{service}/install.sh on this node",
    }


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_TABLE_DDL = """\
CREATE TABLE IF NOT EXISTS embeddings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_type TEXT NOT NULL,
    source_id TEXT NOT NULL,
    chunk INTEGER NOT NULL DEFAULT 0,
    header TEXT NOT NULL DEFAULT '',
    chunk_text TEXT NOT NULL,
    embedding BLOB,
    model TEXT,
    content_hash TEXT,
    updated_at TEXT NOT NULL,
    UNIQUE(source_type, source_id, chunk)
);
"""
_FTS_DDL = (
    "CREATE VIRTUAL TABLE IF NOT EXISTS embeddings_fts "
    "USING fts5(text, tokenize='porter unicode61');\n"
)

# A service appends this to its own DDL for ``init_service_db``. Existing DBs
# are upgraded by :func:`ensure_schema`, which every entry point here calls.
EMBEDDINGS_DDL = _TABLE_DDL + _FTS_DDL

_ensured: set[str] = set()


def _db_key(conn: sqlite3.Connection) -> str:
    for row in conn.execute("PRAGMA database_list"):
        if row[1] == "main":
            return row[2] or ""
    return ""


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create or upgrade the chunk store on ``conn``, idempotently.

    The one-row-per-document table of earlier releases is rebuilt in a single
    transaction. Its rows carry over as chunk 0 with no content hash, so they
    stay searchable until the next :func:`reindex` replaces them.
    """
    key = _db_key(conn)
    if key and key in _ensured:
        return
    cols = {r[1] for r in conn.execute("PRAGMA table_info(embeddings)")}
    has_fts = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name='embeddings_fts'").fetchone()
    if cols and "chunk" in cols and has_fts:
        if key:
            _ensured.add(key)
        return
    if conn.in_transaction:
        conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    try:
        if cols and "chunk" not in cols:
            conn.execute("ALTER TABLE embeddings RENAME TO embeddings_v1")
            conn.execute(_TABLE_DDL)
            conn.execute(
                "INSERT INTO embeddings (source_type, source_id, chunk, header, chunk_text,"
                " embedding, model, content_hash, updated_at)"
                " SELECT source_type, source_id, 0, '', chunk_text, embedding, ?, NULL, updated_at"
                " FROM embeddings_v1", (_LEGACY_MODEL,))
            conn.execute("DROP TABLE embeddings_v1")
        elif not cols:
            conn.execute(_TABLE_DDL)
        conn.execute("DROP TABLE IF EXISTS embeddings_fts")
        conn.execute(_FTS_DDL)
        conn.execute(
            "INSERT INTO embeddings_fts (rowid, text) SELECT id,"
            " CASE WHEN header = '' THEN chunk_text ELSE header || char(10) || chunk_text END"
            " FROM embeddings")
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    if key:
        _ensured.add(key)


# ---------------------------------------------------------------------------
# Embedder
# ---------------------------------------------------------------------------


class Embedder(Protocol):
    name: str

    def count_tokens(self, texts: list[str]) -> list[int]: ...

    def encode(self, texts: list[str], *, query: bool) -> Any: ...


class _SentenceTransformer:
    name = MODEL_NAME

    def __init__(self) -> None:
        self._model = None

    def _load(self):
        if self._model is None:
            try:
                import torch
                from sentence_transformers import SentenceTransformer
            except ImportError as exc:
                raise EmbeddingsUnavailable(f"sentence-transformers: {_INSTALL_HINT}") from exc
            threads = int(os.environ.get("AWM_EMBED_THREADS", "4"))
            if threads < torch.get_num_threads():
                torch.set_num_threads(threads)
            self._model = SentenceTransformer(MODEL_NAME, device="cpu")
        return self._model

    def count_tokens(self, texts: list[str]) -> list[int]:
        ids = self._load().tokenizer(texts, add_special_tokens=False, verbose=False)["input_ids"]
        return [len(x) for x in ids]

    def encode(self, texts: list[str], *, query: bool):
        import numpy as np

        prefix = QUERY_PREFIX if query else DOCUMENT_PREFIX
        vecs = self._load().encode([prefix + t for t in texts], batch_size=32,
                                   normalize_embeddings=True, show_progress_bar=False)
        return np.asarray(vecs, dtype=np.float32)


_embedder: Embedder | None = None


def get_embedder() -> Embedder:
    """The process-wide embedder; raises :class:`EmbeddingsUnavailable` without the stack."""
    global _embedder
    if _embedder is None:
        if not probe()["available"]:
            raise EmbeddingsUnavailable(f"{', '.join(probe()['missing'])}: {_INSTALL_HINT}")
        _embedder = _SentenceTransformer()
    return _embedder


def use_embedder(embedder: Embedder | None) -> None:
    """Replace the process-wide embedder (tests); ``None`` restores the default."""
    global _embedder
    _embedder = embedder


def _try_embedder() -> Embedder | None:
    try:
        return get_embedder()
    except EmbeddingsUnavailable:
        return None


def _approx_tokens(texts: list[str]) -> list[int]:
    return [len(re.findall(r"\w+|[^\w\s]", t)) for t in texts]


# ---------------------------------------------------------------------------
# Chunking
# ---------------------------------------------------------------------------

_HEADING = re.compile(r"^(#{1,6}\s+.+|\*\*[^*\n]{2,80}\*\*:?\s*$)")
_LIST_ITEM = re.compile(r"^([-*+]|\d+[.)])\s")
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+|\n")


@dataclass(frozen=True)
class Document:
    """One searchable item: ``body`` is chunked, the rest forms each chunk's header."""

    source_id: str
    body: str
    title: str = ""
    context: str = ""
    date: str = ""


def _blocks(body: str) -> list[tuple[str, str]]:
    """Split into ``(section, text)``: headings open sections, blank lines and list starts split."""
    section, out, cur = "", [], []

    def flush():
        text = "\n".join(cur).strip()
        if text:
            out.append((section, text))
        cur.clear()

    for line in body.splitlines():
        s = line.strip()
        if _HEADING.match(s):
            flush()
            section = s.strip("#* :").strip()
            continue
        if not s:
            flush()
            continue
        if cur and _LIST_ITEM.match(s) and not _LIST_ITEM.match(cur[-1].strip()):
            flush()
        cur.append(line)
    flush()
    return out


def _pack(units: list[str], counts: list[int], target: int, sep: str) -> list[tuple[str, int]]:
    out, cur, n = [], [], 0
    for u, c in zip(units, counts):
        if cur and n + c > target:
            out.append((sep.join(cur), n))
            cur, n = [], 0
        cur.append(u)
        n += c
    if cur:
        out.append((sep.join(cur), n))
    return out


def _split_long(text: str, target: int, ntok) -> list[tuple[str, int]]:
    """Split an over-budget block at sentence ends, then any over-budget sentence at words."""
    sents = [s for s in _SENTENCE_END.split(text) if s and s.strip()]
    pieces = []
    for sent, n in zip(sents, ntok(sents)):
        if n <= target:
            pieces.append((sent, n))
        else:
            words = sent.split()
            pieces += _pack(words, ntok(words), target, " ")
    return _pack([p for p, _ in pieces], [n for _, n in pieces], target, " ")


def chunk(body: str, target: int = CHUNK_TOKENS,
          ntok: Callable[[list[str]], list[int]] = _approx_tokens) -> list[tuple[str, str]]:
    """Pack structural blocks into ``(section, text)`` chunks of at most ``target`` tokens.

    A block never merges across a section change once the chunk is half full,
    and a chunk only starts mid-block when the block alone is over budget.
    """
    blocks = _blocks(body)
    chunks, cur, cur_sec, cur_n = [], [], "", 0
    for (sec, text), n in zip(blocks, ntok([t for _, t in blocks])):
        parts = [(text, n)] if n <= target else _split_long(text, target, ntok)
        for part, pn in parts:
            if cur and (cur_n + pn > target or (sec != cur_sec and cur_n > target // 2)):
                chunks.append((cur_sec, "\n".join(cur)))
                cur, cur_n = [], 0
            if not cur:
                cur_sec = sec
            cur.append(part)
            cur_n += pn
    if cur:
        chunks.append((cur_sec, "\n".join(cur)))
    return chunks


def header(doc: Document, section: str = "") -> str:
    """``title — context · date — section``, omitting empty parts."""
    parts = [doc.title.strip()[:200]] if doc.title.strip() else []
    meta = " · ".join(x for x in (doc.context, doc.date) if x)
    if meta:
        parts.append(meta)
    if section:
        parts.append(section[:120])
    return " — ".join(parts)


def content_hash(doc: Document) -> str:
    raw = "\x00".join((str(CHUNKER_VERSION), str(CHUNK_TOKENS), doc.title, doc.context,
                       doc.date, doc.body))
    return hashlib.sha1(raw.encode()).hexdigest()


def _chunks_of(doc: Document, ntok) -> list[tuple[str, str]]:
    """``(header, text)`` per chunk; a body-less document indexes its title alone."""
    parts = chunk(doc.body, CHUNK_TOKENS, ntok)
    if not parts:
        return [("", doc.title.strip())] if doc.title.strip() else []
    return [(header(doc, sec), text) for sec, text in parts]


def _joined(head: str, text: str) -> str:
    return f"{head}\n{text}" if head else text


# ---------------------------------------------------------------------------
# Indexing
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write(conn, source_type: str, doc: Document, chash: str,
           rows: list[tuple[str, str]], vecs, model: str | None) -> None:
    _delete(conn, source_type, doc.source_id)
    now = _now_iso()
    for i, (head, text) in enumerate(rows):
        blob = vecs[i].tobytes() if vecs is not None else None
        cur = conn.execute(
            "INSERT INTO embeddings (source_type, source_id, chunk, header, chunk_text,"
            " embedding, model, content_hash, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (source_type, doc.source_id, i, head, text, blob, model, chash, now))
        conn.execute("INSERT INTO embeddings_fts (rowid, text) VALUES (?, ?)",
                     (cur.lastrowid, _joined(head, text)))


def _delete(conn, source_type: str, source_id: str) -> None:
    conn.execute(
        "DELETE FROM embeddings_fts WHERE rowid IN"
        " (SELECT id FROM embeddings WHERE source_type=? AND source_id=?)",
        (source_type, source_id))
    conn.execute("DELETE FROM embeddings WHERE source_type=? AND source_id=?",
                 (source_type, source_id))


def _index_batch(conn, source_type: str, docs: list[Document], emb: Embedder | None) -> None:
    ntok = emb.count_tokens if emb else _approx_tokens
    prepared = [(d, _chunks_of(d, ntok)) for d in docs]
    vecs = None
    if emb:
        texts = [_joined(h, t) for _, rows in prepared for h, t in rows]
        vecs = emb.encode(texts, query=False) if texts else None
    at = 0
    for doc, rows in prepared:
        if not rows:
            _delete(conn, source_type, doc.source_id)
            continue
        part = vecs[at:at + len(rows)] if vecs is not None else None
        at += len(rows)
        _write(conn, source_type, doc, content_hash(doc), rows, part,
               emb.name if emb else None)


def index_document(conn: sqlite3.Connection, source_type: str, doc: Document) -> str:
    """Chunk, embed and store ``doc``; commits. Returns ``unchanged``, ``indexed`` or ``keyword-only``.

    Skips the work when the content hash and model already match. Without the
    search stack the chunks are stored unembedded, which keeps keyword search
    working and marks them for the next :func:`reindex` once the stack exists.
    CPU-bound: call it off the event loop.
    """
    ensure_schema(conn)
    emb = _try_embedder()
    want = emb.name if emb else None
    have = conn.execute(
        "SELECT DISTINCT content_hash, model FROM embeddings WHERE source_type=? AND source_id=?",
        (source_type, doc.source_id)).fetchall()
    if len(have) == 1 and tuple(have[0]) == (content_hash(doc), want):
        return "unchanged"
    _index_batch(conn, source_type, [doc], emb)
    conn.commit()
    return "indexed" if emb else "keyword-only"


def delete_document(conn: sqlite3.Connection, source_type: str, source_id: str) -> None:
    """Remove every chunk of one document; commits."""
    ensure_schema(conn)
    _delete(conn, source_type, source_id)
    conn.commit()


def reindex(conn: sqlite3.Connection, source_type: str, docs: Iterable[Document], *,
            force: bool = False, prune: bool = False, dry_run: bool = False,
            batch_chunks: int = 256) -> dict[str, Any]:
    """Bring ``source_type``'s index in line with ``docs``; returns counts.

    A document is re-embedded when it is missing, changed (content hash),
    stale (another model, or stored unembedded), or when ``force`` is set.
    ``prune`` deletes indexed documents absent from ``docs``, so pass it only
    with the complete set. Work commits every ``batch_chunks`` chunks, so an
    interrupted run keeps what it finished. CPU-bound: run it off the event loop.
    """
    ensure_schema(conn)
    t0 = time.monotonic()
    emb = _try_embedder()
    want = emb.name if emb else ""
    have = {r[0]: tuple(r[1:]) for r in conn.execute(
        "SELECT source_id, MIN(content_hash), MAX(content_hash),"
        " MIN(coalesce(model, '')), MAX(coalesce(model, '')) FROM embeddings"
        " WHERE source_type=? GROUP BY source_id", (source_type,))}
    counts = {"documents": 0, "current": 0, "missing": 0, "changed": 0, "stale": 0,
              "indexed": 0, "pruned": 0}
    todo, seen = [], set()
    for doc in docs:
        counts["documents"] += 1
        seen.add(doc.source_id)
        h = have.get(doc.source_id)
        chash = content_hash(doc)
        if h is None:
            state = "missing"
        elif not (h[0] == h[1] == chash):
            state = "changed"
        elif not (h[2] == h[3] == want):
            state = "stale"
        else:
            state = "current"
        counts[state] += 1
        if state != "current" or force:
            todo.append(doc)
    orphans = [sid for sid in have if sid not in seen] if prune else []
    if not dry_run:
        batch, size = [], 0
        for doc in todo:
            batch.append(doc)
            size += max(1, len(doc.body) // 400)
            if size >= batch_chunks:
                _index_batch(conn, source_type, batch, emb)
                conn.commit()
                counts["indexed"] += len(batch)
                batch, size = [], 0
        if batch:
            _index_batch(conn, source_type, batch, emb)
            counts["indexed"] += len(batch)
        for sid in orphans:
            _delete(conn, source_type, sid)
        counts["pruned"] = len(orphans)
        conn.commit()
    else:
        counts["pruned"] = len(orphans)
    counts["semantic"] = bool(emb)
    counts["seconds"] = round(time.monotonic() - t0, 2)
    return counts


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

_STOPWORDS = frozenset(
    "a an the and or of to in on for with by from at as is are was were be been it its this "
    "that what which who how why when where do does did can i my me we you they not".split())


def fts_query(text: str) -> str:
    """An FTS5 MATCH expression: the query's non-stopword terms, quoted, OR-ed.

    Quoting every term makes any punctuation in ``text`` inert.
    """
    terms = [t for t in re.findall(r"\w+", text.lower()) if t not in _STOPWORDS]
    return " OR ".join(f'"{t}"' for t in dict.fromkeys(terms))


@dataclass
class SearchResult:
    """Ranked hits, best first, plus a ``degraded`` block when only keywords ranked them."""

    hits: list[dict[str, Any]] = field(default_factory=list)
    degraded: dict[str, Any] | None = None


def _filter_sql(allowed: str | None) -> str:
    return f" AND e.source_id IN ({allowed})" if allowed else ""


def _keyword(conn, query: str, source_type: str, allowed: str | None,
             params: tuple) -> dict[str, tuple[float, int]]:
    expr = fts_query(query)
    if not expr:
        return {}
    best: dict[str, tuple[float, int]] = {}
    for sid, ch, score in conn.execute(
            "SELECT e.source_id, e.chunk, bm25(embeddings_fts) FROM embeddings_fts"
            " JOIN embeddings e ON e.id = embeddings_fts.rowid"
            " WHERE embeddings_fts MATCH ? AND e.source_type = ?" + _filter_sql(allowed),
            (expr, source_type, *params)):
        if sid not in best or -score > best[sid][0]:
            best[sid] = (-score, ch)
    return best


def _dense(conn, query: str, source_type: str, allowed: str | None, params: tuple,
           emb: Embedder) -> dict[str, tuple[float, int]]:
    import numpy as np

    rows = conn.execute(
        "SELECT e.source_id, e.chunk, e.embedding FROM embeddings e"
        " WHERE e.source_type = ? AND e.model = ? AND e.embedding IS NOT NULL"
        + _filter_sql(allowed), (source_type, emb.name, *params)).fetchall()
    if not rows:
        return {}
    qv = emb.encode([query], query=True)[0]
    rows = [r for r in rows if len(r[2]) == qv.nbytes]
    if not rows:
        return {}
    mat = np.frombuffer(b"".join(r[2] for r in rows), dtype=np.float32).reshape(len(rows), -1)
    scores = mat @ qv
    best: dict[str, tuple[float, int]] = {}
    for (sid, ch, _), s in zip(rows, scores.tolist()):
        if sid not in best or s > best[sid][0]:
            best[sid] = (s, ch)
    return best


def _minmax_top(scores: dict[str, float], depth: int) -> dict[str, float]:
    top = sorted(scores.values(), reverse=True)[:depth]
    if not top:
        return {}
    hi, lo = top[0], top[-1]
    return {d: (v - lo) / (hi - lo) if hi > lo else 1.0 for d, v in scores.items() if v >= lo}


def fuse(dense: dict[str, float], keyword: dict[str, float], alpha: float = ALPHA,
         depth: int = FUSION_DEPTH) -> dict[str, float]:
    """``alpha * dense + (1 - alpha) * keyword``, each min-max normalised over its top ``depth``."""
    a, b = _minmax_top(dense, depth), _minmax_top(keyword, depth)
    return {d: alpha * a.get(d, 0.0) + (1 - alpha) * b.get(d, 0.0) for d in a.keys() | b.keys()}


def _service_name(conn) -> str:
    return Path(_db_key(conn)).stem or "<service>"


def search(conn: sqlite3.Connection, query: str, *, source_type: str,
           allowed: str | None = None, params: Iterable[Any] = (),
           limit: int = 10) -> SearchResult:
    """Rank ``source_type``'s documents against ``query``, best first.

    ``allowed`` is a SQL ``SELECT`` over the caller's own tables returning the
    permitted source ids as text, with ``params`` bound to its placeholders
    (for example ``"SELECT id FROM scope_posts WHERE kind = ?"``). Only those
    documents are scored, so a narrow filter still returns its best matches.
    Each hit carries ``source_id``, ``score``, and its best ``chunk`` with
    that chunk's text as ``snippet``.
    """
    ensure_schema(conn)
    params = tuple(params)
    if not query.strip():
        return SearchResult()
    degraded = None
    emb = _try_embedder()
    dense: dict[str, tuple[float, int]] = {}
    if emb is None:
        degraded = degraded_marker(_service_name(conn), fallback="keyword")
    else:
        dense = _dense(conn, query, source_type, allowed, params, emb)
    keyword = _keyword(conn, query, source_type, allowed, params)
    fused = fuse({d: s for d, (s, _) in dense.items()},
                 {d: s for d, (s, _) in keyword.items()})
    ranked = sorted(fused, key=lambda d: (-fused[d], d))[:limit]
    hits = []
    for sid in ranked:
        ch = (dense.get(sid) or keyword[sid])[1]
        row = conn.execute(
            "SELECT chunk_text FROM embeddings WHERE source_type=? AND source_id=? AND chunk=?",
            (source_type, sid, ch)).fetchone()
        hits.append({"source_id": sid, "score": round(fused[sid], 4), "chunk": ch,
                     "snippet": row[0] if row else ""})
    return SearchResult(hits, degraded)


def semantic_search(conn: sqlite3.Connection, query: str, source_type: str,
                    limit: int = 10, *, allowed: str | None = None,
                    params: Iterable[Any] = ()) -> list[dict[str, Any]]:
    """Dense-only MaxP search: ``source_type``, ``source_id``, ``chunk_text``, cosine ``score``.

    For a caller that blends raw cosine into its own score; ``allowed`` and
    ``params`` filter first, as in :func:`search`.
    """
    ensure_schema(conn)
    dense = _dense(conn, query, source_type, allowed, tuple(params), get_embedder())
    out = []
    for sid in sorted(dense, key=lambda d: -dense[d][0])[:limit]:
        score, ch = dense[sid]
        row = conn.execute(
            "SELECT chunk_text FROM embeddings WHERE source_type=? AND source_id=? AND chunk=?",
            (source_type, sid, ch)).fetchone()
        out.append({"source_type": source_type, "source_id": sid,
                    "chunk_text": (row[0] if row else "")[:200], "score": round(score, 4)})
    return out
