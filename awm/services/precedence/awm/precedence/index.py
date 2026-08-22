"""Embedding index — per-field reuse of the workspace-standard embedding engine.

``awm.persistence.embeddings`` is the same module behind the other services'
semantic search (all-MiniLM-L6-v2, 384-dim, normalized; stored as sqlite-vec
BLOBs, queried via ``vec_distance_cosine``). We reuse it verbatim so the archive
shares the workspace's embedding stack rather than introducing a parallel one.

Unlike the other services (one embedding per row), a precedence decision is
embedded **per field** — three rows per decision, one under each of
``config.SOURCE_TYPES`` (context / question / decision), all keyed by the same
``source_id`` (the decision id). This is what lets a caller query any subset of
the entry shape and score each field's match independently.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from awm.persistence.embeddings import (  # noqa: F401  (EMBEDDINGS_DDL re-exported)
    EMBEDDINGS_DDL,
    EmbeddingsUnavailable,
    degraded_marker,
    embed_text,
    probe,
    upsert_embedding,
    delete_embedding,
    semantic_search,
)

from . import config


def embed_decision(
    conn: sqlite3.Connection, did: str, context: str, question: str, decision: str
) -> None:
    """(Re-)embed all three fields of one decision under their source-type namespaces."""
    fields = {"context": context, "question": question, "decision": decision}
    for field, text in fields.items():
        upsert_embedding(conn, config.SOURCE_TYPES[field], did, text or "")


def drop_decision(conn: sqlite3.Connection, did: str) -> None:
    """Delete all three per-field embedding rows for a decision."""
    for source_type in config.SOURCE_TYPES.values():
        delete_embedding(conn, source_type, did)


def search_field(
    conn: sqlite3.Connection, field: str, query: str, limit: int = 50
) -> list[dict[str, Any]]:
    """Nearest decisions to ``query`` within one field's embedding namespace.

    Returns dicts with ``source_id`` (the decision id) and ``score`` (cosine sim).
    """
    return semantic_search(conn, query, source_type=config.SOURCE_TYPES[field], limit=limit)
