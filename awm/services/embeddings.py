"""Semantic search via sqlite-vec + sentence-transformers."""

from __future__ import annotations

import struct
from datetime import datetime, timezone
from typing import Any

from awm.db import get_connection

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
# Embedding + indexing
# ---------------------------------------------------------------------------


def embed_text(text: str) -> list[float]:
    """Generate an embedding vector for the given text."""
    model = _get_model()
    return model.encode(text, normalize_embeddings=True).tolist()


def _upsert_embedding(source_type: str, source_id: str, text: str) -> None:
    """Embed text and store/update in the embeddings table."""
    vec = embed_text(text)
    blob = _vec_to_blob(vec)
    now = _now_iso()

    conn = get_connection()
    try:
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
    finally:
        conn.close()


def index_skill(skill_path: str) -> None:
    """Embed a skill file for semantic search."""
    from awm.services.skills import get_skill
    try:
        result = get_skill(skill_path)
        # Combine searchable fields
        skill = result.skill
        text = f"{skill.name} {skill.description} {' '.join(skill.tags)} {result.content[:500]}"
        _upsert_embedding("skill", skill_path, text)
    except FileNotFoundError:
        pass


def index_session(session_id: int) -> None:
    """Embed a session log entry for semantic search. ``session_id`` is the
    public ``legacy_id`` minted by the local peer; v32 keeps the public id
    INTEGER, so this signature is unchanged."""
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT summary, deviations, suggestions "
            "FROM session_logs WHERE legacy_id=? "
            "AND origin_peer = COALESCE("
            "  (SELECT peer_id FROM peers WHERE ssh_alias = 'self' LIMIT 1), '')",
            (session_id,),
        ).fetchone()
    finally:
        conn.close()

    if row:
        parts = [row["summary"] or ""]
        if row["deviations"]:
            parts.append(row["deviations"])
        if row["suggestions"]:
            parts.append(row["suggestions"])
        text = " ".join(parts)
        _upsert_embedding("session", str(session_id), text)


def index_artifact(artifact_id: int) -> None:
    """Embed an artifact for semantic search. ``artifact_id`` is the public
    legacy_id of a local-origin artifact (remote-origin artifacts are
    indexed on their owning peer; we'd duplicate the work otherwise)."""
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT name, description, tags FROM artifacts "
            "WHERE legacy_id=? AND origin_peer = "
            "COALESCE((SELECT peer_id FROM peers WHERE ssh_alias='self' LIMIT 1), '')",
            (artifact_id,),
        ).fetchone()
    finally:
        conn.close()

    if row:
        parts = [row["name"] or ""]
        if row["description"]:
            parts.append(row["description"])
        if row["tags"]:
            parts.append(row["tags"])
        text = " ".join(parts)
        _upsert_embedding("artifact", str(artifact_id), text)


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


def semantic_search(
    query: str,
    source_type: str | None = None,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Find nearest neighbors by cosine similarity.

    Returns list of dicts with keys: source_type, source_id, chunk_text, score.
    """
    query_vec = embed_text(query)
    query_blob = _vec_to_blob(query_vec)

    conn = get_connection()
    try:
        # Load sqlite-vec extension
        import sqlite_vec
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)

        # Build the virtual table query
        # sqlite-vec uses vec_distance_cosine for similarity
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
    finally:
        conn.close()

    results = []
    for r in rows:
        results.append({
            "source_type": r[0],
            "source_id": r[1],
            "chunk_text": r[2][:200],
            "score": round(1.0 - r[3], 4),  # convert distance to similarity
        })
    return results


# ---------------------------------------------------------------------------
# Bulk reindex
# ---------------------------------------------------------------------------


def reindex_all() -> dict:
    """Full rebuild: force skills + artifacts sync, re-embed all experiences, drop stray rows.

    This is the heavy hammer — prefer `skills.sync_skills()` / `artifacts.sync_artifacts()`
    for routine, lazy syncing. Use this when you need to guarantee the embeddings table
    is a complete mirror of current state, including experiences (which are append-only
    and therefore have no sync function of their own).
    """
    from awm.services.skills import sync_skills
    from awm.services.artifacts import sync_artifacts

    stats: dict = {
        "skills": sync_skills(force=True),
        "artifacts": sync_artifacts(force=True),
        "sessions": 0,
        "stray_pruned": 0,
    }

    # Sessions — upsert all session log entries.
    conn = get_connection()
    try:
        # Only index local-origin sessions; replicated remote rows are
        # indexed on their owning peer.
        session_ids = [r[0] for r in conn.execute(
            "SELECT legacy_id FROM session_logs WHERE origin_peer = "
            "COALESCE((SELECT peer_id FROM peers WHERE ssh_alias = 'self' LIMIT 1), '')"
        ).fetchall()]
    finally:
        conn.close()

    for sid in session_ids:
        try:
            index_session(sid)
            stats["sessions"] += 1
        except Exception:
            pass

    # Final safety pass: drop embeddings rows whose source_type we don't recognize
    # (e.g. legacy schema drift). sync_skills / sync_artifacts already handle their
    # own source_types; this just catches orphans with unknown types.
    known_types = {"skill", "artifact", "session"}
    conn = get_connection()
    try:
        rows = conn.execute("SELECT source_type, source_id FROM embeddings").fetchall()
        stray = [(t, s) for t, s in rows if t not in known_types]
        if stray:
            conn.executemany(
                "DELETE FROM embeddings WHERE source_type=? AND source_id=?",
                stray,
            )
            conn.commit()
            stats["stray_pruned"] = len(stray)
    finally:
        conn.close()

    return stats
