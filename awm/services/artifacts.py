"""Artifact registry — track outputs across scopes (v37).

Artifacts no longer replicate cross-peer in v37; the legacy
``origin_peer``/``legacy_id`` plumbing is gone and the INTEGER PK is
authoritative for a single workspace. Phase-6 cross-peer fetch becomes a
future fetch-not-sync design.
"""

from __future__ import annotations

from awm.config import WORKSPACE_ROOT
from awm.db import get_connection
from awm.models import (
    ArtifactRegisterRequest,
    ArtifactInfo,
    ArtifactSearchResponse,
)
from awm.services import identity
from awm.services._validation import validate_name
from awm.services.identity import ms_to_iso, now_ms


def _row_to_info(row) -> ArtifactInfo:
    return ArtifactInfo(
        id=row["id"],
        project=row["project_name"],
        scope=row["scope_name"],
        name=row["name"],
        artifact_type=row["artifact_type"],
        path=row["path"],
        description=row["description"],
        format=row["format"],
        tags=row["tags"],
        status=row["status"],
        created_at=ms_to_iso(row["created_at"]) or "",
        origin_peer="",
    )


_BASE_SELECT = (
    "SELECT a.*, p.name AS project_name, ag.scope AS scope_name "
    "FROM artifacts a "
    "JOIN agents ag ON ag.id = a.agent_id "
    "JOIN projects p ON p.id = ag.project_id"
)


def _ensure_agent_for_artifact(project: str, scope: str, *, conn) -> str:
    """Resolve (project, scope) → agent id, materializing missing rows."""
    pid = identity.project_id_for_name(project, conn=conn)
    if pid is None:
        from awm.config import PROJECTS_DIR
        bare = PROJECTS_DIR / project / ".bare"
        identity.ensure_project(project, repo_path=str(bare), conn=conn)
    return identity.ensure_agent(project, scope, conn=conn)


def register_artifact(req: ArtifactRegisterRequest) -> ArtifactInfo:
    """Upserts on (path, agent's project) — two scopes in the same project
    cannot independently register the same path. Cross-project collisions
    are allowed (each project owns its own filesystem subtree)."""
    validate_name(req.project, kind="project name")
    validate_name(req.scope, kind="scope name")
    conn = get_connection()
    try:
        agent_id = _ensure_agent_for_artifact(req.project, req.scope, conn=conn)
        project_id = identity.project_id_for_name(req.project, conn=conn)
        existing = conn.execute(
            "SELECT a.id FROM artifacts a "
            "JOIN agents ag ON ag.id = a.agent_id "
            "WHERE a.path = ? AND ag.project_id = ? LIMIT 1",
            (req.path, project_id),
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE artifacts SET "
                " agent_id=?, name=?, artifact_type=?, description=?, "
                " format=?, tags=?, status='current', updated_at=? "
                "WHERE id=?",
                (agent_id, req.name, req.artifact_type, req.description,
                 req.format, req.tags, now_ms(), existing[0]),
            )
            target_id = existing[0]
        else:
            cur = conn.execute(
                "INSERT INTO artifacts "
                "(agent_id, name, artifact_type, path, description, "
                " format, tags, status, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 'current', ?, ?)",
                (agent_id, req.name, req.artifact_type, req.path,
                 req.description, req.format, req.tags,
                 now_ms(), now_ms()),
            )
            target_id = cur.lastrowid
        conn.commit()
        row = conn.execute(f"{_BASE_SELECT} WHERE a.id=?", (target_id,)).fetchone()
        info = _row_to_info(row)
    finally:
        conn.close()

    try:
        from awm.services.embeddings import index_artifact
        index_artifact(info.id)
    except Exception:
        pass

    return info


def delete_artifact(artifact_id: int | str) -> dict:
    try:
        aid = int(str(artifact_id).split("@", 1)[0])
    except ValueError as exc:
        raise ValueError(f"Artifact {artifact_id} not found") from exc
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT id, name, path FROM artifacts WHERE id=?", (aid,),
        ).fetchone()
        if not row:
            raise ValueError(f"Artifact {artifact_id} not found")
        conn.execute("DELETE FROM artifacts WHERE id=?", (aid,))
        conn.execute(
            "DELETE FROM embeddings WHERE source_type='artifact' AND source_id=?",
            (str(aid),),
        )
        conn.commit()
        return {"deleted": True, "id": aid, "name": row["name"], "path": row["path"]}
    finally:
        conn.close()


def search_artifacts(
    project: str | None = None,
    scope: str | None = None,
    artifact_type: str | None = None,
    query: str | None = None,
    limit: int = 50,
) -> ArtifactSearchResponse:
    conn = get_connection()
    try:
        sql = f"{_BASE_SELECT} WHERE a.status='current'"
        params: list = []
        if project:
            sql += " AND p.name = ?"
            params.append(project)
        if scope:
            sql += " AND ag.scope = ?"
            params.append(scope)
        if artifact_type:
            sql += " AND a.artifact_type = ?"
            params.append(artifact_type)
        if query:
            sql += " AND (a.name LIKE ? OR a.description LIKE ? OR a.tags LIKE ?)"
            like = f"%{query}%"
            params.extend([like, like, like])
        sql += " ORDER BY p.name, a.artifact_type, a.name LIMIT ?"
        params.append(limit)
        rows = conn.execute(sql, params).fetchall()
        items = [_row_to_info(r) for r in rows]
        return ArtifactSearchResponse(artifacts=items, total=len(items))
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Content read — v37: local-only, no federation routing
# ---------------------------------------------------------------------------

class ArtifactNotFound(Exception):
    pass


class ArtifactContentUnavailable(Exception):
    pass


def _read_local_content(path: str) -> bytes:
    full = WORKSPACE_ROOT / path
    if not full.exists():
        raise ArtifactNotFound(f"file missing on disk: {path}")
    return full.read_bytes()


def get_content(artifact_ref: int | str) -> bytes:
    try:
        aid = int(str(artifact_ref).split("@", 1)[0])
    except ValueError as exc:
        raise ArtifactNotFound(f"artifact {artifact_ref} not found") from exc
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT path FROM artifacts WHERE id=?", (aid,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        raise ArtifactNotFound(f"artifact {artifact_ref} not found")
    return _read_local_content(row["path"])


# ---------------------------------------------------------------------------
# Sync — flip artifact status based on on-disk presence and prune embeddings
# ---------------------------------------------------------------------------

_SYNC_FP_KEY = "artifacts_sync_fp"


def _fingerprint_artifacts_db() -> str:
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT COALESCE(MAX(updated_at), 0), COUNT(*) FROM artifacts"
        ).fetchone()
    finally:
        conn.close()
    return f"{row[0]}|{row[1]}"


def sync_artifacts(force: bool = False) -> dict:
    from awm.services.config_service import get_config, set_config

    fp = _fingerprint_artifacts_db()
    if not force and get_config(_SYNC_FP_KEY) == fp:
        return {"skipped": True, "reason": "fingerprint_unchanged"}

    marked_stale: list[int] = []
    restored: list[int] = []
    restored_ids: list[int] = []
    stale_embeddings: list[str] = []

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
                restored_ids.append(aid)

        if marked_stale:
            conn.executemany(
                "UPDATE artifacts SET status='stale', updated_at=? WHERE id=?",
                [(now_ms(), aid) for aid in marked_stale],
            )
        if restored:
            conn.executemany(
                "UPDATE artifacts SET status='current', updated_at=? WHERE id=?",
                [(now_ms(), aid) for aid in restored],
            )

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

    if restored_ids:
        try:
            from awm.services.embeddings import index_artifact
            for aid in restored_ids:
                try:
                    index_artifact(aid)
                except Exception:
                    pass
        except Exception:
            pass

    set_config(_SYNC_FP_KEY, _fingerprint_artifacts_db())
    return {
        "skipped": False,
        "marked_stale": len(marked_stale),
        "restored": len(restored),
        "embeddings_pruned": len(stale_embeddings),
    }
