"""Notes in the shared retrieval engine (``awm.persistence.embeddings``).

The service owns its own ``embeddings`` table (per the per-service DB
invariant); these helpers namespace note rows under ``config.SOURCE_TYPE`` and
use the note's path as the title every chunk carries.
"""

from __future__ import annotations

import logging
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

log = logging.getLogger(__name__)


def embed_note(conn: sqlite3.Connection, note_id: str, text: str) -> None:
    """Index a note; raises :class:`EmbeddingsUnavailable` when only keywords could be stored."""
    row = conn.execute("SELECT path FROM notes WHERE id=?", (note_id,)).fetchone()
    doc = engine.Document(note_id, text, title=(row[0] if row else "") or "")
    if engine.index_document(conn, config.SOURCE_TYPE, doc) == "keyword-only":
        raise EmbeddingsUnavailable(f"{', '.join(probe()['missing'])}: semantic stack missing")


def reembed(conn: sqlite3.Connection, note_id: str, text: str, content_hash: str) -> bool:
    """(Re)embed a note and stamp ``embedded_hash``, best-effort.

    Embedding is the one part of a write that depends on a heavy optional stack
    (``awm-persistence[search]``). When that stack is missing or the model call
    fails, the note's content, title and keyword index must still land — so a failure is logged and swallowed, and
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
    engine.delete_document(conn, config.SOURCE_TYPE, note_id)


def not_current(conn: sqlite3.Connection) -> set[str]:
    """Live notes whose index rows are missing or were made by another model."""
    model = engine.get_embedder().name
    return {r[0] for r in conn.execute(
        "SELECT id FROM notes WHERE deleted_at IS NULL AND id NOT IN"
        " (SELECT source_id FROM embeddings WHERE source_type=? AND model=?"
        "  AND content_hash IS NOT NULL)", (config.SOURCE_TYPE, model))}


def search_semantic(conn: sqlite3.Connection, query: str, limit: int = 50, *,
                    include_trashed: bool = False) -> list[dict[str, Any]]:
    """Ranked ``{source_id, score, snippet}``; raises :class:`EmbeddingsUnavailable` without the stack."""
    allowed = "SELECT id FROM notes" + ("" if include_trashed else " WHERE deleted_at IS NULL")
    res = engine.search(conn, query, source_type=config.SOURCE_TYPE, allowed=allowed, limit=limit)
    if res.degraded:
        raise EmbeddingsUnavailable(f"{', '.join(res.degraded['missing'])}: semantic stack missing")
    return res.hits
