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


def index_experience(experience_id: int) -> None:
    """Embed an experience for semantic search."""
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT summary, deviations, suggestions FROM experiences WHERE id=?",
            (experience_id,),
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
        _upsert_embedding("experience", str(experience_id), text)


def index_artifact(artifact_id: int) -> None:
    """Embed an artifact for semantic search."""
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT name, description, tags FROM artifacts WHERE id=?",
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
    """Rebuild all embeddings from current data."""
    from awm.services.skills import list_skills

    stats = {"skills": 0, "experiences": 0, "artifacts": 0}

    # Skills
    skill_result = list_skills()
    for skill in skill_result.skills:
        try:
            index_skill(skill.file_path)
            stats["skills"] += 1
        except Exception:
            pass

    # Experiences
    conn = get_connection()
    try:
        exp_ids = [r[0] for r in conn.execute("SELECT id FROM experiences").fetchall()]
        art_ids = [r[0] for r in conn.execute("SELECT id FROM artifacts").fetchall()]
    finally:
        conn.close()

    for eid in exp_ids:
        try:
            index_experience(eid)
            stats["experiences"] += 1
        except Exception:
            pass

    for aid in art_ids:
        try:
            index_artifact(aid)
            stats["artifacts"] += 1
        except Exception:
            pass

    return stats
