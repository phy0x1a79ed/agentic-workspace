"""Embedding index — reuse of the workspace-standard embedding engine.

``awm.persistence.embeddings`` is the same stack behind the other services'
semantic search (all-MiniLM-L6-v2, 384-dim, normalized; sqlite-vec BLOBs, cosine
via ``vec_distance_cosine``). The service owns its own ``embeddings`` table (per
the per-service DB invariant); these helpers namespace note rows under
``config.SOURCE_TYPE``.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from awm.persistence.embeddings import (  # noqa: F401  (re-exported)
    EMBEDDINGS_DDL,
    delete_embedding,
    embed_text,
    semantic_search,
    upsert_embedding,
)

from . import config


def embed_note(conn: sqlite3.Connection, note_id: str, text: str) -> None:
    upsert_embedding(conn, config.SOURCE_TYPE, note_id, text)


def drop_embedding(conn: sqlite3.Connection, note_id: str) -> None:
    delete_embedding(conn, config.SOURCE_TYPE, note_id)


def search_semantic(conn: sqlite3.Connection, query: str, limit: int = 50) -> list[dict[str, Any]]:
    return semantic_search(conn, query, source_type=config.SOURCE_TYPE, limit=limit)
