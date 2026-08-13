"""Embedding index — reuse of the workspace-standard embedding engine.

``awm.persistence.embeddings`` is the same stack behind the other services'
semantic search (all-MiniLM-L6-v2, 384-dim, normalized; sqlite-vec BLOBs, cosine
via ``vec_distance_cosine``). The service owns its own ``embeddings`` table (per
the per-service DB invariant); these helpers namespace note rows under
``config.SOURCE_TYPE``.
"""

from __future__ import annotations

import logging
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

log = logging.getLogger(__name__)


def embed_note(conn: sqlite3.Connection, note_id: str, text: str) -> None:
    upsert_embedding(conn, config.SOURCE_TYPE, note_id, text)


def reembed(conn: sqlite3.Connection, note_id: str, text: str, content_hash: str) -> bool:
    """(Re)embed a note and stamp ``embedded_hash``, best-effort.

    Embedding is the one part of a write that depends on a heavy optional stack
    (``awm-persistence[search]``: sentence-transformers + sqlite-vec). When that
    stack is missing or the model call fails, the note's content, title and
    keyword index must still land — so a failure is logged and swallowed, and
    ``embedded_hash`` is left stale so the next write retries. Returns whether
    the embedding is now current.
    """
    try:
        if text.strip():
            embed_note(conn, note_id, text)
        else:
            drop_embedding(conn, note_id)   # nothing to embed; drop any stale vector
    except Exception:  # noqa: BLE001 — indexing must never fail the write
        log.warning("notes: embedding unavailable, leaving %s unindexed", note_id, exc_info=True)
        return False
    conn.execute("UPDATE notes SET embedded_hash=? WHERE id=?", (content_hash, note_id))
    return True


def drop_embedding(conn: sqlite3.Connection, note_id: str) -> None:
    delete_embedding(conn, config.SOURCE_TYPE, note_id)


def search_semantic(conn: sqlite3.Connection, query: str, limit: int = 50) -> list[dict[str, Any]]:
    return semantic_search(conn, query, source_type=config.SOURCE_TYPE, limit=limit)
