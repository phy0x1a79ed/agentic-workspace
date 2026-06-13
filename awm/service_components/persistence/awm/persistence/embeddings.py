"""Semantic-search engine: embed text, store/query a per-service ``embeddings`` table.

Engine only. Under per-service DBs each feature service that wants search owns
its own copy of the tiny ``embeddings`` table (DDL in :data:`EMBEDDINGS_DDL`)
and its own embeddings rows. The functions here take the caller's connection
so they operate on that service's own DB — they do NOT reach into any specific
feature's tables. Feature-specific indexers (``index_skill`` / ``index_session``
/ ``index_artifact`` / …) and the cross-service ``reindex_all`` were removed:
each belongs to the owning feature service, which reimplements it against its
own DAO.

``sentence-transformers`` / ``sqlite-vec`` are imported lazily so a service
that never searches doesn't pay for them (and the dist's ``search`` extra need
not be installed).
"""

from __future__ import annotations

import sqlite3
import struct
from datetime import datetime, timezone
from typing import Any

# DDL for the per-service embeddings table. Each service that indexes installs
# its own copy via ``init_service_db`` (register it alongside the service's own
# tables). Identical to the legacy shared shape so seed-from-state.db is a
# straight copy.
EMBEDDINGS_DDL = """\
CREATE TABLE IF NOT EXISTS embeddings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_type TEXT NOT NULL,
    source_id TEXT NOT NULL,
    chunk_text TEXT NOT NULL,
    embedding BLOB NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(source_type, source_id)
);
"""

# ---------------------------------------------------------------------------
# Lazy model loading
# ---------------------------------------------------------------------------

_model = None
_MODEL_NAME = "all-MiniLM-L6-v2"
_VEC_DIM = 384  # dimension for all-MiniLM-L6-v2


def _get_model():
    """Lazy-load the sentence-transformers model on first use."""
    global _model
    if _model is None:
        from sentence_transformers import SentenceTransformer
        _model = SentenceTransformer(_MODEL_NAME)
    return _model


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _vec_to_blob(vec: list[float]) -> bytes:
    """Pack a float vector into a binary blob for sqlite-vec."""
    return struct.pack(f"{len(vec)}f", *vec)


def _blob_to_vec(blob: bytes) -> list[float]:
    """Unpack a binary blob back to a float vector."""
    n = len(blob) // 4
    return list(struct.unpack(f"{n}f", blob))


# ---------------------------------------------------------------------------
# Embedding + per-service upsert
# ---------------------------------------------------------------------------


def embed_text(text: str) -> list[float]:
    """Generate an embedding vector for the given text."""
    model = _get_model()
    return model.encode(text, normalize_embeddings=True).tolist()


def upsert_embedding(
    conn: sqlite3.Connection,
    source_type: str,
    source_id: str,
    text: str,
) -> None:
    """Embed ``text`` and store/update it in the caller's ``embeddings`` table.

    Operates on the connection the caller hands in (its own per-service DB).
    Commits on the passed connection.
    """
    vec = embed_text(text)
    blob = _vec_to_blob(vec)
    now = _now_iso()
    conn.execute(
        """INSERT INTO embeddings (source_type, source_id, chunk_text, embedding, updated_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(source_type, source_id) DO UPDATE SET
            chunk_text=excluded.chunk_text,
            embedding=excluded.embedding,
            updated_at=excluded.updated_at""",
        (source_type, source_id, text, blob, now),
    )
    conn.commit()


def delete_embedding(
    conn: sqlite3.Connection, source_type: str, source_id: str
) -> None:
    """Remove an embeddings row from the caller's ``embeddings`` table.

    Used by category remove hooks so the embeddings table stays in sync with
    category state. Operates on the passed connection; commits on it.
    """
    conn.execute(
        "DELETE FROM embeddings WHERE source_type=? AND source_id=?",
        (source_type, source_id),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


def semantic_search(
    conn: sqlite3.Connection,
    query: str,
    source_type: str | None = None,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Find nearest neighbours by cosine similarity in ``conn``'s embeddings table.

    Queries the caller's own per-service ``embeddings`` table. Returns a list of
    dicts with keys: source_type, source_id, chunk_text, score.
    """
    query_vec = embed_text(query)
    query_blob = _vec_to_blob(query_vec)

    # Load sqlite-vec extension on the caller's connection (lazy import).
    import sqlite_vec
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)

    # sqlite-vec uses vec_distance_cosine for similarity.
    if source_type:
        sql = """
            SELECT e.source_type, e.source_id, e.chunk_text,
                   vec_distance_cosine(e.embedding, ?) as distance
            FROM embeddings e
            WHERE e.source_type = ?
            ORDER BY distance ASC
            LIMIT ?
        """
        rows = conn.execute(sql, (query_blob, source_type, limit)).fetchall()
    else:
        sql = """
            SELECT e.source_type, e.source_id, e.chunk_text,
                   vec_distance_cosine(e.embedding, ?) as distance
            FROM embeddings e
            ORDER BY distance ASC
            LIMIT ?
        """
        rows = conn.execute(sql, (query_blob, limit)).fetchall()

    results = []
    for r in rows:
        results.append({
            "source_type": r[0],
            "source_id": r[1],
            "chunk_text": r[2][:200],
            "score": round(1.0 - r[3], 4),  # convert distance to similarity
        })
    return results


def hybrid_augment(
    conn: sqlite3.Connection,
    query: str,
    *,
    source_type: str,
    keyword_hits: list,
    keyword_keys: set[str],
    materialize,
    semantic_limit: int = 10,
    semantic_threshold: float = 0.3,
) -> list:
    """Return keyword_hits, then a semantic top-up from ``conn``'s embeddings.

    Any :func:`semantic_search` hit whose source_id is not already in
    ``keyword_keys`` and whose similarity exceeds the threshold is appended.
    Mirrors the mixing rule originally inlined in ``skills.search_skills()`` so
    every category uses the same heuristic.

    ``materialize(source_id) -> row | None`` re-applies the caller's structural
    filter so a semantically-relevant row that's been filtered out (wrong
    project, wrong status) doesn't surface via the semantic path.

    Degrades gracefully to keyword-only when sentence-transformers / sqlite-vec
    aren't importable, or when semantic_search raises.
    """
    if not query:
        return list(keyword_hits)
    out = list(keyword_hits)
    try:
        hits = semantic_search(conn, query, source_type=source_type, limit=semantic_limit)
    except Exception:
        return out
    for h in hits:
        sid = h["source_id"]
        if sid in keyword_keys:
            continue
        if h["score"] <= semantic_threshold:
            continue
        try:
            row = materialize(sid)
        except Exception:
            row = None
        if row is None:
            continue
        out.append(row)
    return out
