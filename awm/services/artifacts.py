"""Artifact registry — track outputs across scopes.

After v34, artifact rows replicate (cr-sqlite) across all peers. File
content does NOT replicate — only the metadata does. Phase 6 federates
content reads: ``get_content(ref)`` consults the row's ``origin_peer``
and either reads the local file or proxies to that peer's
``GET /peer/artifacts/{legacy_id}/content`` endpoint.
"""

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
from awm.services._validation import validate_name
from awm.services.network import peers as peer_svc
from awm.services.replication.schema import new_uuid, next_legacy_id


def _local_peer_id() -> str:
    try:
        ident = peer_svc.get_local_identity()
    except Exception:
        return ""
    if ident is None:
        return ""
    return ident.get("peer_id") or ""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_to_info(row) -> ArtifactInfo:
    return ArtifactInfo(
        id=row["legacy_id"],
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
        origin_peer=row["origin_peer"] if "origin_peer" in row.keys() else "",
    )


def register_artifact(req: ArtifactRegisterRequest) -> ArtifactInfo:
    """Register or update an artifact. Upserts on (path, origin_peer=local).

    Cross-peer rows (origin_peer != self) are intentionally NOT matched here:
    each peer manages its own copies, replication propagates the metadata,
    and content federation handles reads. So the upsert is scoped to local
    rows only — two peers can independently register the same path."""
    validate_name(req.project, kind="project name")
    validate_name(req.scope, kind="scope name")
    now = _now_iso()
    origin = _local_peer_id()
    conn = get_connection()
    try:
        existing = conn.execute(
            "SELECT uuid FROM artifacts WHERE path = ? AND origin_peer = ?",
            (req.path, origin),
        ).fetchone()

        if existing:
            conn.execute(
                """UPDATE artifacts SET
                    project=?, scope=?, name=?, artifact_type=?,
                    description=?, format=?, tags=?, status='current', updated_at=?
                WHERE uuid=?""",
                (req.project, req.scope, req.name, req.artifact_type,
                 req.description, req.format, req.tags, now, existing["uuid"]),
            )
            target_uuid = existing["uuid"]
        else:
            legacy = next_legacy_id(conn, "artifacts", origin)
            target_uuid = new_uuid()
            conn.execute(
                """INSERT INTO artifacts
                    (uuid, legacy_id, origin_peer, project, scope, name,
                     artifact_type, path, description, format, tags, status,
                     created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'current', ?, ?)""",
                (target_uuid, legacy, origin, req.project, req.scope, req.name,
                 req.artifact_type, req.path, req.description, req.format,
                 req.tags, now, now),
            )

        conn.commit()
        row = conn.execute(
            "SELECT * FROM artifacts WHERE uuid = ?", (target_uuid,),
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


def delete_artifact(artifact_id: int | str) -> dict:
    """Permanently delete an artifact and its embeddings. Accepts ``42``
    or ``'42@peer'`` to disambiguate post-replication rows."""
    legacy_id, peer = peer_svc.parse_id_ref(artifact_id)
    origin = peer if peer is not None else _local_peer_id()
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT uuid, name, path FROM artifacts "
            "WHERE legacy_id = ? AND origin_peer = ?",
            (legacy_id, origin),
        ).fetchone()
        if not row:
            raise ValueError(f"Artifact {artifact_id} not found")

        uid = row["uuid"]
        conn.execute("DELETE FROM artifacts WHERE uuid = ?", (uid,))
        conn.execute(
            "DELETE FROM embeddings WHERE source_type = 'artifact' AND source_id = ?",
            (str(legacy_id),),
        )
        conn.commit()
        return {"deleted": True, "id": legacy_id, "name": row["name"],
                "path": row["path"]}
    finally:
        conn.close()


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
# Content read with phase-6 federation
# ---------------------------------------------------------------------------

class ArtifactNotFound(Exception):
    """Raised when a get_content lookup matches no row."""


class ArtifactContentUnavailable(Exception):
    """Raised when the owning peer is unreachable or returns an error."""


def _read_local_content(path: str) -> bytes:
    """Return the raw bytes of a workspace-relative artifact path. Caller
    has already confirmed origin_peer == local."""
    full = WORKSPACE_ROOT / path
    if not full.exists():
        raise ArtifactNotFound(f"file missing on disk: {path}")
    return full.read_bytes()


def get_content(artifact_ref: int | str) -> bytes:
    """Return the raw bytes for an artifact.

    ``artifact_ref`` is the public legacy_id (``42``) or a peer-scoped
    ref (``'42@capella'``). If the row's ``origin_peer`` equals the local
    peer (or is empty, meaning pre-federation local), the file is read
    from disk. Otherwise the call federates to the owning peer's
    ``GET /peer/artifacts/{legacy_id}/content``.

    Raises ``ArtifactNotFound`` (no matching row) or
    ``ArtifactContentUnavailable`` (federation failed)."""
    legacy_id, peer = peer_svc.parse_id_ref(artifact_ref)
    origin_filter = peer if peer is not None else None

    conn = get_connection()
    try:
        if origin_filter is None:
            # Bare id: prefer the local-origin row if present, else any.
            row = conn.execute(
                "SELECT origin_peer, path FROM artifacts "
                "WHERE legacy_id = ? AND origin_peer = ?",
                (legacy_id, _local_peer_id()),
            ).fetchone()
            if row is None:
                row = conn.execute(
                    "SELECT origin_peer, path FROM artifacts WHERE legacy_id = ?",
                    (legacy_id,),
                ).fetchone()
        else:
            row = conn.execute(
                "SELECT origin_peer, path FROM artifacts "
                "WHERE legacy_id = ? AND origin_peer = ?",
                (legacy_id, origin_filter),
            ).fetchone()
    finally:
        conn.close()

    if row is None:
        raise ArtifactNotFound(f"artifact {artifact_ref} not found")

    owner = row["origin_peer"]
    local = _local_peer_id()
    if not owner or owner == local:
        return _read_local_content(row["path"])

    # Remote origin — federate.
    from awm.services.network import federation
    try:
        return federation.forward_artifact_content(owner, legacy_id)
    except federation.FederationError as exc:
        raise ArtifactContentUnavailable(str(exc)) from exc


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
    marked_stale: list[str] = []
    restored: list[str] = []
    restored_legacy_ids: list[int] = []
    origin = _local_peer_id()

    conn = get_connection()
    try:
        # Only sync local-origin rows; remote-origin rows point at files on
        # another peer's filesystem and should not be flipped to 'stale'
        # just because they don't exist here.
        rows = conn.execute(
            "SELECT uuid, legacy_id, path, status FROM artifacts "
            "WHERE origin_peer = ?",
            (origin,),
        ).fetchall()

        for r in rows:
            uid, rel_path, status = r["uuid"], r["path"], r["status"]
            exists = (WORKSPACE_ROOT / rel_path).exists()
            if status == "current" and not exists:
                marked_stale.append(uid)
            elif status == "stale" and exists:
                restored.append(uid)
                restored_legacy_ids.append(r["legacy_id"])

        if marked_stale:
            conn.executemany(
                "UPDATE artifacts SET status='stale', updated_at=? WHERE uuid=?",
                [(now, u) for u in marked_stale],
            )
        if restored:
            conn.executemany(
                "UPDATE artifacts SET status='current', updated_at=? WHERE uuid=?",
                [(now, u) for u in restored],
            )

        # Prune embeddings for any artifact that isn't currently 'current'.
        # Embeddings.source_id is the local-origin legacy_id (we never embed
        # remote-origin artifact metadata locally).
        current_ids = {
            str(r[0]) for r in conn.execute(
                "SELECT legacy_id FROM artifacts "
                "WHERE status='current' AND origin_peer = ?",
                (origin,),
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
    if restored_legacy_ids:
        try:
            from awm.services.embeddings import index_artifact
            for aid in restored_legacy_ids:
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
