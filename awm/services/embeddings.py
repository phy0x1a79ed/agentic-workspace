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


def delete_embedding(source_type: str, source_id: str) -> None:
    """Remove an embeddings row. Used by release/remove hooks (locks, peers)
    so the embeddings table stays in sync with category state."""
    conn = get_connection()
    try:
        conn.execute(
            "DELETE FROM embeddings WHERE source_type=? AND source_id=?",
            (source_type, source_id),
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
            "FROM session_logs WHERE legacy_id=? AND origin_peer = ''",
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
            "WHERE legacy_id=? AND origin_peer = ''",
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


def index_scope(project: str, scope: str) -> None:
    """Embed a scope for semantic search. Combines the scope identifier with
    the first 500 chars of its `.awm/context.md` (if present) so 'find the
    auth scope' matches both scopes named *auth* and scopes whose context
    talks about authentication."""
    from awm.config import PROJECTS_DIR
    source_id = f"{project}/{scope}"
    parts = [source_id]
    ctx = PROJECTS_DIR / project / scope / ".awm" / "context.md"
    if ctx.is_file():
        try:
            parts.append(ctx.read_text(encoding="utf-8")[:500])
        except (OSError, UnicodeDecodeError):
            pass
    _upsert_embedding("scope", source_id, "\n".join(parts))


def index_room(room_id: str) -> None:
    """Embed a room: topic + tail of post bodies. Re-run periodically as the
    transcript grows so semantic search tracks the evolving discussion."""
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT topic FROM rooms WHERE id = ?", (room_id,),
        ).fetchone()
        if row is None:
            return
        posts = conn.execute(
            "SELECT body FROM room_posts WHERE room_id = ? "
            "ORDER BY id DESC LIMIT 20",
            (room_id,),
        ).fetchall()
    finally:
        conn.close()

    parts = [row["topic"] or room_id]
    for p in reversed(posts):
        if p["body"]:
            parts.append(p["body"])
    text = "\n".join(parts)[:2000]
    _upsert_embedding("room", room_id, text)


def index_project(project: str) -> None:
    """Embed a project: name + first 500 chars of its README, if any. Walks
    standard README locations under the project root."""
    from awm.config import PROJECTS_DIR
    parts = [project]
    proj_dir = PROJECTS_DIR / project
    for candidate in ("README.md", "README.rst", "README", "README.txt"):
        readme = proj_dir / candidate
        if readme.is_file():
            try:
                parts.append(readme.read_text(encoding="utf-8")[:500])
            except (OSError, UnicodeDecodeError):
                pass
            break
    _upsert_embedding("project", project, "\n".join(parts))


def index_lock(lock_id: int) -> None:
    """Embed an active lock: resource path + holder + metadata. Deleted on
    release/reap."""
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT id, resource_path, holder_id, metadata FROM locks WHERE id = ?",
            (lock_id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return
    parts = [row["resource_path"] or "", row["holder_id"] or ""]
    if row["metadata"]:
        parts.append(str(row["metadata"]))
    _upsert_embedding("lock", str(row["id"]), " ".join(p for p in parts if p))


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


def hybrid_augment(
    query: str,
    *,
    source_type: str,
    keyword_hits: list,
    keyword_keys: set[str],
    materialize,
    semantic_limit: int = 10,
    semantic_threshold: float = 0.3,
) -> list:
    """Return keyword_hits, then semantic top-up: any semantic_search() hits
    whose source_id is not already in keyword_keys and whose similarity score
    exceeds the threshold. Mirrors the mixing rule originally inlined in
    skills.search_skills() so every category uses the same heuristic.

    `materialize(source_id) -> row | None` re-applies the caller's structural
    filter so a semantically-relevant row that's been filtered out (wrong
    project, wrong status) doesn't surface via the semantic path.

    Degrades gracefully to keyword-only when sentence-transformers /
    sqlite-vec aren't importable, or when semantic_search raises.
    """
    if not query:
        return list(keyword_hits)
    out = list(keyword_hits)
    try:
        hits = semantic_search(query, source_type=source_type, limit=semantic_limit)
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


# ---------------------------------------------------------------------------
# Bulk reindex
# ---------------------------------------------------------------------------


def reindex_all() -> dict:
    """Full rebuild: force skills + artifacts sync, re-embed every other
    category, drop stray rows.

    This is the heavy hammer — prefer `skills.sync_skills()` /
    `artifacts.sync_artifacts()` for routine lazy syncing. Use this when you
    need the embeddings table to be a complete mirror of current state.
    """
    from awm.services.skills import sync_skills
    from awm.services.artifacts import sync_artifacts

    stats: dict = {
        "skills": sync_skills(force=True),
        "artifacts": sync_artifacts(force=True),
        "sessions": 0,
        "scopes": 0,
        "rooms": 0,
        "projects": 0,
        "locks": 0,
        "stray_pruned": 0,
    }

    conn = get_connection()
    try:
        session_ids = [r[0] for r in conn.execute(
            "SELECT legacy_id FROM session_logs WHERE origin_peer = ''"
        ).fetchall()]
        scope_rows = conn.execute(
            "SELECT project, scope FROM scopes WHERE status != 'deleted'"
        ).fetchall()
        room_ids = [r[0] for r in conn.execute(
            "SELECT id FROM rooms"
        ).fetchall()]
        lock_ids = [r[0] for r in conn.execute(
            "SELECT id FROM locks"
        ).fetchall()]
    finally:
        conn.close()

    for sid in session_ids:
        try:
            index_session(sid)
            stats["sessions"] += 1
        except Exception:
            pass

    for r in scope_rows:
        try:
            index_scope(r["project"], r["scope"])
            stats["scopes"] += 1
        except Exception:
            pass

    for rid in room_ids:
        try:
            index_room(rid)
            stats["rooms"] += 1
        except Exception:
            pass

    from awm.config import PROJECTS_DIR
    if PROJECTS_DIR.is_dir():
        for child in sorted(PROJECTS_DIR.iterdir()):
            if child.is_dir() and (child / ".bare").is_dir():
                try:
                    index_project(child.name)
                    stats["projects"] += 1
                except Exception:
                    pass

    for lid in lock_ids:
        try:
            index_lock(lid)
            stats["locks"] += 1
        except Exception:
            pass

    # Final safety pass: drop embeddings rows whose source_type we don't
    # recognize (legacy schema drift).
    known_types = {"skill", "artifact", "session", "scope", "room", "project", "lock"}
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
