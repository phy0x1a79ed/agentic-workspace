"""Embedding index — thin reuse of the workspace-standard embedding engine.

``awm.persistence.embeddings`` is the same module behind the other services'
semantic search (all-MiniLM-L6-v2, 384-dim, normalized; stored as sqlite-vec
BLOBs, queried via ``vec_distance_cosine``). We reuse it verbatim so the corpus
shares the workspace's embedding stack rather than introducing a parallel one.
The service owns its own ``embeddings`` table (per the per-service DB invariant);
these helpers namespace corpus rows under ``config.SOURCE_TYPE``.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from awm.persistence.embeddings import (  # noqa: F401  (re-exported)
    EMBEDDINGS_DDL,
    embed_text,
    upsert_embedding,
    delete_embedding,
    semantic_search,
)

from . import config


def embed_sample(conn: sqlite3.Connection, sample_id: str, text: str) -> None:
    """Embed one sample's text under the corpus source-type namespace."""
    upsert_embedding(conn, config.SOURCE_TYPE, sample_id, text)


def drop_embedding(conn: sqlite3.Connection, sample_id: str) -> None:
    delete_embedding(conn, config.SOURCE_TYPE, sample_id)


def search_semantic(conn: sqlite3.Connection, query: str, limit: int = 50) -> list[dict[str, Any]]:
    """Nearest neighbours within the corpus, by cosine similarity."""
    return semantic_search(conn, query, source_type=config.SOURCE_TYPE, limit=limit)


def pairwise_cosine(conn: sqlite3.Connection) -> list[tuple[str, str, float]]:
    """All sample-pair cosine similarities, descending. Cheap for ~hundreds of rows.

    Uses sqlite-vec's ``vec_distance_cosine`` over the stored embeddings so we
    never re-run the model. Returns (id_a, id_b, similarity) with id_a < id_b.
    """
    import sqlite_vec

    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    rows = conn.execute(
        """
        SELECT a.source_id AS ida, b.source_id AS idb,
               1.0 - vec_distance_cosine(a.embedding, b.embedding) AS sim
        FROM embeddings a
        JOIN embeddings b
          ON a.source_type = b.source_type
         AND a.source_id < b.source_id
        WHERE a.source_type = ?
        ORDER BY sim DESC
        """,
        (config.SOURCE_TYPE,),
    ).fetchall()
    return [(r[0], r[1], round(r[2], 4)) for r in rows]
