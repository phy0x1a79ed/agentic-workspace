"""Artifact registry — track outputs across scopes."""

from __future__ import annotations

from datetime import datetime, timezone

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
