"""Artifact registry — track outputs across scopes (modular v1).

Re-keyed to natural keys: ``project`` / ``scope`` are native columns on
``artifacts``; the legacy ``agent_id`` FK is gone. Identity is validated by
calling the ``scopes`` service over gateway RPC (via ``gatewayclient``), cached
in a module-level ``RefCache``. Embeddings are per-service — this module calls
into the ``persistence.embeddings`` engine against the artifacts service's own
connection.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

from awm.config import WORKSPACE_ROOT
from awm.gatewayclient import RefCache, GatewayCallError, call_sync
from awm.artifacts.dao import ArtifactsDAO, init as dao_init
from awm.artifacts.models import (
    ArtifactRegisterRequest,
    ArtifactInfo,
    ArtifactSearchResponse,
)

# ---------------------------------------------------------------------------
# Time helpers (re-homed from identity — no import from scopes)
# ---------------------------------------------------------------------------


def now_ms() -> int:
    """Current time as unix milliseconds."""
    return int(time.time() * 1000)


def ms_to_iso(ms: int | None) -> str | None:
    """Convert unix-ms to an ISO 8601 string, or None for falsy input."""
    if not ms:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Validation helper (inline copy — avoids importing from scopes)
# ---------------------------------------------------------------------------

_FORBIDDEN_CHARS = ("/", "\\", "\x00")


def _validate_name(name: str, kind: str = "name") -> str:
    if not isinstance(name, str) or not name:
        raise ValueError(f"{kind} must be a non-empty string")
    if name in (".", ".."):
        raise ValueError(f"{kind} cannot be '.' or '..'")
    if name.startswith("."):
        raise ValueError(f"{kind} cannot start with '.' (got {name!r})")
    for ch in _FORBIDDEN_CHARS:
        if ch in name:
            raise ValueError(f"{kind} cannot contain {ch!r} (got {name!r})")
    return name


# ---------------------------------------------------------------------------
# Module-level RefCache for resolveScope calls
# ---------------------------------------------------------------------------

_scope_cache: RefCache = RefCache(ttl=60.0)


def _resolve_scope(project: str, scope: str) -> dict | None:
    """Call scopes.resolveScope synchronously via the gateway; cache positives.

    Returns the result dict ``{exists, project, scope, status}`` on success,
    or None / falsy on "not found". Raises ``GatewayCallError`` on transport
    failure.
    """
    # RefCache.validate is async; we use call_sync here since artifacts.py
    # functions are sync (called from the ServiceAdapter thread pool).
    result = call_sync("scopes", "resolveScope", {"project": project, "scope": scope})
    # gateway returns {} for null — normalise falsy/empty to None
    if not result or not result.get("exists"):
        return None
    return result


# ---------------------------------------------------------------------------
# Row → model
# ---------------------------------------------------------------------------


def _row_to_info(row: dict) -> ArtifactInfo:
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
        created_at=ms_to_iso(row["created_at"]) or "",
        origin_peer="",
    )


# ---------------------------------------------------------------------------
# Per-service artifact indexer
# ---------------------------------------------------------------------------


def _index_artifact(artifact_id: int) -> None:
    """Embed the artifact's text and upsert into this service's embeddings table.

    Degrades gracefully when sentence-transformers / sqlite-vec are not installed.
    """
    try:
        from awm.persistence.embeddings import upsert_embedding
        from awm.persistence.databases import get_connection
    except ImportError:
        return

    dao = ArtifactsDAO()
    row = dao.get_by_id(artifact_id)
    if not row:
        return

    parts = [row["name"] or "", row["artifact_type"] or "", row["description"] or "",
             row["tags"] or ""]
    text = " ".join(p for p in parts if p).strip()
    if not text:
        return

    try:
        from awm.persistence.databases import get_connection as _gc
        conn = _gc("artifacts")
        try:
            upsert_embedding(conn, "artifact", str(artifact_id), text)
        finally:
            conn.close()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Public API — register
# ---------------------------------------------------------------------------


def register_artifact(req: ArtifactRegisterRequest) -> ArtifactInfo:
    """Upserts on (path, project) — validate (project, scope) via scopes RPC
    BEFORE writing. On unresolvable scope, fail LOUDLY — no orphan rows."""
    _validate_name(req.project, kind="project name")
    _validate_name(req.scope, kind="scope name")

    # RPC-validate: reject unresolvable scopes
    try:
        resolved = _resolve_scope(req.project, req.scope)
    except GatewayCallError as exc:
        raise ValueError(
            f"Could not validate scope {req.project!r}/{req.scope!r} "
            f"via scopes service: {exc}"
        ) from exc

    if not resolved:
        raise ValueError(
            f"Scope {req.project!r}/{req.scope!r} does not exist. "
            "Register the scope with the scopes service before registering artifacts."
        )

    dao = ArtifactsDAO()
    with dao.transaction() as conn:
        existing = dao.get_by_path_and_project(req.path, req.project, conn=conn)
        ts = now_ms()
        if existing:
            dao.update_artifact(
                existing["id"],
                project=req.project,
                scope=req.scope,
                name=req.name,
                artifact_type=req.artifact_type,
                description=req.description,
                format=req.format,
                tags=req.tags,
                updated_at=ts,
                conn=conn,
            )
            target_id = existing["id"]
        else:
            target_id = dao.insert_artifact(
                project=req.project,
                scope=req.scope,
                name=req.name,
                artifact_type=req.artifact_type,
                path=req.path,
                description=req.description,
                format=req.format,
                tags=req.tags,
                created_at=ts,
                updated_at=ts,
                conn=conn,
            )
        row = dao.get_by_id(target_id, conn=conn)

    info = _row_to_info(row)

    try:
        _index_artifact(info.id)
    except Exception:
        pass

    return info


# ---------------------------------------------------------------------------
# Public API — delete
# ---------------------------------------------------------------------------


def delete_artifact(artifact_id: int | str) -> dict:
    try:
        aid = int(str(artifact_id).split("@", 1)[0])
    except ValueError as exc:
        raise ValueError(f"Artifact {artifact_id} not found") from exc

    dao = ArtifactsDAO()
    with dao.transaction() as conn:
        row = dao.delete_artifact(aid, conn=conn)
        if not row:
            raise ValueError(f"Artifact {artifact_id} not found")
        dao.delete_embedding_by_source_id(str(aid), conn=conn)

    return {"deleted": True, "id": aid, "name": row["name"], "path": row["path"]}


# ---------------------------------------------------------------------------
# Public API — search
# ---------------------------------------------------------------------------


def search_artifacts(
    project: str | None = None,
    scope: str | None = None,
    artifact_type: str | None = None,
    query: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> ArtifactSearchResponse:
    """Search/filter artifacts. When ``query`` is set, returns keyword (LIKE on
    name/description/tags) hits first, then semantic top-up via this service's
    own embeddings table."""
    dao = ArtifactsDAO()
    rows = dao.search(
        project=project,
        scope=scope,
        artifact_type=artifact_type,
        query=query,
        limit=limit,
        offset=offset,
    )
    items = [_row_to_info(r) for r in rows]

    if not query:
        return ArtifactSearchResponse(artifacts=items, total=len(items))

    keyword_keys = {str(i.id) for i in items}

    def _materialize(source_id: str) -> ArtifactInfo | None:
        try:
            aid = int(source_id)
        except ValueError:
            return None
        row = dao.get_by_id(aid)
        if row is None or row.get("status") != "current":
            return None
        if project and row["project"] != project:
            return None
        if scope and row["scope"] != scope:
            return None
        if artifact_type and row["artifact_type"] != artifact_type:
            return None
        return _row_to_info(row)

    try:
        from awm.persistence.embeddings import hybrid_augment
        from awm.persistence.databases import get_connection
        conn = get_connection("artifacts")
        try:
            merged = hybrid_augment(
                conn, query,
                source_type="artifact",
                keyword_hits=items,
                keyword_keys=keyword_keys,
                materialize=_materialize,
            )
        finally:
            conn.close()
    except Exception:
        merged = items

    return ArtifactSearchResponse(artifacts=merged, total=len(merged))


# ---------------------------------------------------------------------------
# Content read — local-only, no federation
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
    dao = ArtifactsDAO()
    path = dao.get_path_by_id(aid)
    if path is None:
        raise ArtifactNotFound(f"artifact {artifact_ref} not found")
    return _read_local_content(path)


# ---------------------------------------------------------------------------
# Sync — flip artifact status based on on-disk presence, prune embeddings
# ---------------------------------------------------------------------------

_SYNC_FP_KEY = "artifacts_sync_fp"


def sync_artifacts(force: bool = False) -> dict:
    from awm.persistence.config_service import get_config, set_config

    dao = ArtifactsDAO()
    fp = dao.get_fingerprint()
    if not force and get_config(_SYNC_FP_KEY) == fp:
        return {"skipped": True, "reason": "fingerprint_unchanged"}

    marked_stale: list[int] = []
    restored: list[int] = []
    restored_ids: list[int] = []

    with dao.transaction() as conn:
        rows = dao.get_all_for_sync(conn=conn)

        for r in rows:
            aid, rel_path, status = r["id"], r["path"], r["status"]
            exists = (WORKSPACE_ROOT / rel_path).exists()
            if status == "current" and not exists:
                marked_stale.append(aid)
            elif status == "stale" and exists:
                restored.append(aid)
                restored_ids.append(aid)

        ts = now_ms()
        if marked_stale:
            dao.mark_stale(marked_stale, ts, conn=conn)
        if restored:
            dao.restore_current(restored, ts, conn=conn)

        # Prune embeddings for artifacts that are no longer current
        current_ids = dao.get_current_ids(conn=conn)
        embedding_ids = dao.get_embedding_source_ids(conn=conn)
        stale_embeddings = [sid for sid in embedding_ids if sid not in current_ids]
        if stale_embeddings:
            dao.delete_stale_embeddings(stale_embeddings, conn=conn)

    if restored_ids:
        for aid in restored_ids:
            try:
                _index_artifact(aid)
            except Exception:
                pass

    set_config(_SYNC_FP_KEY, dao.get_fingerprint())
    return {
        "skipped": False,
        "marked_stale": len(marked_stale),
        "restored": len(restored),
        "embeddings_pruned": len(stale_embeddings) if stale_embeddings else 0,
    }
