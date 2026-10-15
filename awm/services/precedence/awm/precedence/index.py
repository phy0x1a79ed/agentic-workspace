"""Precedence decisions in the shared retrieval engine (``awm.persistence.embeddings``).

Unlike the other services (one document per row), a precedence decision is
indexed **per field** — one document per decision under each of
``config.SOURCE_TYPES`` (context / question / decision), all keyed by the same
``source_id`` (the decision id). This is what lets a caller query any subset of
the entry shape and score each field's match independently.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from awm.persistence import embeddings as engine
from awm.persistence.embeddings import (  # noqa: F401  (EMBEDDINGS_DDL re-exported)
    EMBEDDINGS_DDL,
    EmbeddingsUnavailable,
    degraded_marker,
    probe,
)

from . import config


def embed_decision(
    conn: sqlite3.Connection, did: str, context: str, question: str, decision: str
) -> None:
    """(Re-)index all three fields of one decision; raises :class:`EmbeddingsUnavailable` without the stack."""
    engine.get_embedder()
    fields = {"context": context, "question": question, "decision": decision}
    for field, text in fields.items():
        engine.index_document(conn, config.SOURCE_TYPES[field], engine.Document(did, text or ""))


def drop_decision(conn: sqlite3.Connection, did: str) -> None:
    """Delete all three per-field documents for a decision."""
    for source_type in config.SOURCE_TYPES.values():
        engine.delete_document(conn, source_type, did)


def not_current(conn: sqlite3.Connection) -> set[str]:
    """Decisions with a field whose index rows are missing or were made by another model."""
    model = engine.get_embedder().name
    out: set[str] = set()
    for source_type in config.SOURCE_TYPES.values():
        out |= {r[0] for r in conn.execute(
            "SELECT id FROM decisions WHERE id NOT IN"
            " (SELECT source_id FROM embeddings WHERE source_type=? AND model=?"
            "  AND content_hash IS NOT NULL)", (source_type, model))}
    return out


def search_field(
    conn: sqlite3.Connection, field: str, query: str, limit: int = 50,
    allowed: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Nearest decisions to ``query`` within one field, among ``allowed`` ids.

    Returns dicts with ``source_id`` (the decision id) and ``score`` (cosine sim).
    """
    if allowed is None:
        return engine.semantic_search(conn, query, config.SOURCE_TYPES[field], limit)
    return engine.semantic_search(
        conn, query, config.SOURCE_TYPES[field], limit,
        allowed="SELECT value FROM json_each(?)", params=(json.dumps(sorted(allowed)),))
