"""Artifact registry — track outputs across scopes."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from awm.config import WORKSPACE_ROOT
from awm.db import get_connection
from awm.models import (
    ArtifactRegisterRequest,
    ArtifactInfo,
    ArtifactSearchResponse,
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_to_info(row) -> ArtifactInfo:
    return ArtifactInfo(
        id=row["id"],
        project=row["project"],
        scope=row["scope"],
        name=row["name"],
        artifact_type=row["artifact_type"],
        path=row["path"],
        description=row["description"],
        format=row["format"],
        tags=row["tags"],
        status=row["status"],
        created_at=row["created_at"],
    )


def register_artifact(req: ArtifactRegisterRequest) -> ArtifactInfo:
    """Register or update an artifact. Upserts on path."""
    now = _now_iso()
    conn = get_connection()
    try:
        # Upsert: if path already exists, update metadata
        existing = conn.execute(
            "SELECT id FROM artifacts WHERE path = ?", (req.path,)
        ).fetchone()

        if existing:
            conn.execute(
                """UPDATE artifacts SET
                    project=?, scope=?, name=?, artifact_type=?,
                    description=?, format=?, tags=?, status='current', updated_at=?
                WHERE id=?""",
                (req.project, req.scope, req.name, req.artifact_type,
                 req.description, req.format, req.tags, now, existing["id"]),
            )
        else:
            conn.execute(
                """INSERT INTO artifacts
                    (project, scope, name, artifact_type, path, description,
                     format, tags, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'current', ?, ?)""",
                (req.project, req.scope, req.name, req.artifact_type,
                 req.path, req.description, req.format, req.tags, now, now),
            )

        conn.commit()
        row = conn.execute(
            "SELECT * FROM artifacts WHERE path = ?", (req.path,)
        ).fetchone()
        info = _row_to_info(row)
    finally:
        conn.close()

    # Auto-index for semantic search (best-effort)
    try:
        from awm.services.embeddings import index_artifact
        index_artifact(info.id)
    except Exception:
        pass

    return info


def search_artifacts(
    project: str | None = None,
    scope: str | None = None,
    artifact_type: str | None = None,
    query: str | None = None,
    limit: int = 50,
) -> ArtifactSearchResponse:
    """Search/filter artifacts."""
    conn = get_connection()
    try:
        sql = "SELECT * FROM artifacts WHERE status = 'current'"
        params: list = []
        if project:
            sql += " AND project = ?"
            params.append(project)
        if scope:
            sql += " AND scope = ?"
            params.append(scope)
        if artifact_type:
            sql += " AND artifact_type = ?"
            params.append(artifact_type)
        if query:
            sql += " AND (name LIKE ? OR description LIKE ? OR tags LIKE ?)"
            like = f"%{query}%"
            params.extend([like, like, like])
        sql += " ORDER BY project, artifact_type, name LIMIT ?"
        params.append(limit)

        rows = conn.execute(sql, params).fetchall()
        items = [_row_to_info(r) for r in rows]
        return ArtifactSearchResponse(artifacts=items, total=len(items))
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Sync — flip artifact status based on on-disk presence and prune embeddings.
# ---------------------------------------------------------------------------

_SYNC_FP_KEY = "artifacts_sync_fp"


def _fingerprint_artifacts_db() -> str:
    """DB-only fingerprint for lazy skip.

    Captures `(max(updated_at), COUNT(*))` across the artifacts table. This
    catches all insert/update activity but cannot detect out-of-band file
    deletions — that case falls through to `force=True`. Keeping the happy
    path to three cheap SQL aggregates is the point.
    """
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT COALESCE(MAX(updated_at), ''), COUNT(*) FROM artifacts"
        ).fetchone()
    finally:
        conn.close()
    return f"{row[0]}|{row[1]}"


def sync_artifacts(force: bool = False) -> dict:
    """Sync artifact status and embeddings with on-disk reality.

    Lazy: skips the full walk when the DB fingerprint is unchanged since the
    last sync (common case when nothing was registered or updated). Pass
    `force=True` to bypass the fingerprint — useful at session end when files
    may have been deleted out-of-band without any DB write.

    When running: for each `current` artifact, stats its `path` relative to
    `WORKSPACE_ROOT` and flips to `stale` if the file is gone. For each
    `stale` artifact, flips back to `current` if the file has reappeared.
    Prunes embeddings for any artifact that is no longer `current` and
    returns summary stats.
    """
    from awm.services.config_service import get_config, set_config

    fp = _fingerprint_artifacts_db()
    if not force and get_config(_SYNC_FP_KEY) == fp:
        return {"skipped": True, "reason": "fingerprint_unchanged"}

    now = _now_iso()
    marked_stale: list[int] = []
    restored: list[int] = []

    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT id, path, status FROM artifacts"
        ).fetchall()

        for r in rows:
            aid, rel_path, status = r["id"], r["path"], r["status"]
            exists = (WORKSPACE_ROOT / rel_path).exists()
            if status == "current" and not exists:
                marked_stale.append(aid)
            elif status == "stale" and exists:
                restored.append(aid)

        if marked_stale:
            conn.executemany(
                "UPDATE artifacts SET status='stale', updated_at=? WHERE id=?",
                [(now, aid) for aid in marked_stale],
            )
        if restored:
            conn.executemany(
                "UPDATE artifacts SET status='current', updated_at=? WHERE id=?",
                [(now, aid) for aid in restored],
            )

        # Prune embeddings for any artifact that isn't currently 'current'.
        current_ids = {
            str(r[0]) for r in conn.execute(
                "SELECT id FROM artifacts WHERE status='current'"
            ).fetchall()
        }
        embedding_ids = [
            r[0] for r in conn.execute(
                "SELECT source_id FROM embeddings WHERE source_type='artifact'"
            ).fetchall()
        ]
        stale_embeddings = [sid for sid in embedding_ids if sid not in current_ids]
        if stale_embeddings:
            conn.executemany(
                "DELETE FROM embeddings WHERE source_type='artifact' AND source_id=?",
                [(sid,) for sid in stale_embeddings],
            )

        conn.commit()
    finally:
        conn.close()

    # Re-index restored artifacts so their embeddings come back.
    if restored:
        try:
            from awm.services.embeddings import index_artifact
            for aid in restored:
                try:
                    index_artifact(aid)
                except Exception:
                    pass
        except Exception:
            pass

    # Fingerprint updates above may themselves have bumped updated_at, so
    # recompute after the writes.
    set_config(_SYNC_FP_KEY, _fingerprint_artifacts_db())
    return {
        "skipped": False,
        "marked_stale": len(marked_stale),
        "restored": len(restored),
        "embeddings_pruned": len(stale_embeddings),
    }
