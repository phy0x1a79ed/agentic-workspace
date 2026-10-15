"""The writing corpus in the shared retrieval engine (``awm.persistence.embeddings``).

The service owns its own ``embeddings`` table (per the per-service DB
invariant); these helpers namespace corpus rows under ``config.SOURCE_TYPE`` and
use each sample's name and note as the title its chunks carry.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from awm.persistence import embeddings as engine
from awm.persistence.embeddings import (  # noqa: F401  (re-exported)
    EMBEDDINGS_DDL,
    EmbeddingsUnavailable,
    degraded_marker,
    probe,
)

from . import config


def embed_sample(conn: sqlite3.Connection, sample_id: str, text: str) -> None:
    """Index one sample; raises :class:`EmbeddingsUnavailable` when only keywords could be stored."""
    row = conn.execute("SELECT name, note FROM samples WHERE id=?", (sample_id,)).fetchone()
    title = " — ".join(x for x in (row[0], row[1]) if x) if row else ""
    doc = engine.Document(sample_id, text, title=title)
    if engine.index_document(conn, config.SOURCE_TYPE, doc) == "keyword-only":
        raise EmbeddingsUnavailable(f"{', '.join(probe()['missing'])}: semantic stack missing")


def drop_embedding(conn: sqlite3.Connection, sample_id: str) -> None:
    engine.delete_document(conn, config.SOURCE_TYPE, sample_id)


def not_current(conn: sqlite3.Connection) -> set[str]:
    """Samples whose index rows are missing or were made by another model."""
    model = engine.get_embedder().name
    return {r[0] for r in conn.execute(
        "SELECT id FROM samples WHERE id NOT IN"
        " (SELECT source_id FROM embeddings WHERE source_type=? AND model=?"
        "  AND content_hash IS NOT NULL)", (config.SOURCE_TYPE, model))}


def search_semantic(conn: sqlite3.Connection, query: str, limit: int = 50, *,
                    allowed: str | None = None, params: tuple = ()) -> list[dict[str, Any]]:
    """Ranked ``{source_id, score, snippet}`` among ``allowed`` samples.

    Raises :class:`EmbeddingsUnavailable` without the stack.
    """
    res = engine.search(conn, query, source_type=config.SOURCE_TYPE, allowed=allowed,
                        params=params, limit=limit)
    if res.degraded:
        raise EmbeddingsUnavailable(f"{', '.join(res.degraded['missing'])}: semantic stack missing")
    return res.hits


def pairwise_cosine(conn: sqlite3.Connection) -> list[tuple[str, str, float]]:
    """All sample-pair cosine similarities, descending, without running the model.

    A sample's vector is the normalised mean of its chunk vectors. Returns
    ``(id_a, id_b, similarity)`` with ``id_a < id_b``. Raises
    :class:`EmbeddingsUnavailable` without the stack.
    """
    import numpy as np

    model = engine.get_embedder().name
    chunks: dict[str, list[bytes]] = {}
    for sid, blob in conn.execute(
            "SELECT source_id, embedding FROM embeddings WHERE source_type=? AND model=?"
            " AND embedding IS NOT NULL", (config.SOURCE_TYPE, model)):
        chunks.setdefault(sid, []).append(blob)
    ids = sorted(chunks)
    if len(ids) < 2:
        return []
    vecs = np.stack([np.frombuffer(b"".join(chunks[i]), dtype=np.float32)
                     .reshape(len(chunks[i]), -1).mean(axis=0) for i in ids])
    vecs /= np.maximum(np.linalg.norm(vecs, axis=1, keepdims=True), 1e-9)
    sims = vecs @ vecs.T
    a, b = np.triu_indices(len(ids), k=1)
    order = np.argsort(-sims[a, b], kind="stable")
    return [(ids[a[i]], ids[b[i]], round(float(sims[a[i], b[i]]), 4)) for i in order]
