"""Artifact registry — track outputs across scopes (modular v1).

Re-keyed to natural keys: ``project`` / ``scope`` are native columns on
``artifacts``; the legacy ``agent_id`` FK is gone. Identity is validated by
calling the ``scopes`` service over gateway RPC (via ``gatewayclient``), cached
in a module-level ``RefCache``. Embeddings are per-service — this module calls
into the ``persistence.embeddings`` engine against the artifacts service's own
connection.
"""

from __future__ import annotations

import logging
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

log = logging.getLogger(__name__)

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
_FORBIDDEN_SEGMENT_CHARS = ("\\", "\x00")


def _validate_name(name: str, kind: str = "name", *, allow_nesting: bool = False) -> str:
    """Mirror of ``awm.scopes._validation.validate_name`` — kept in step by hand
    so artifacts stays its own dist. Scope names nest (``fabfos/dev``); project
    names do not, since a slashed project would imply a second bare repo."""
    if not isinstance(name, str) or not name:
        raise ValueError(f"{kind} must be a non-empty string")
    if allow_nesting:
        if name.startswith("/") or name.endswith("/"):
            raise ValueError(f"{kind} cannot start or end with '/' (got {name!r})")
        segments, forbidden = name.split("/"), _FORBIDDEN_SEGMENT_CHARS
    else:
        segments, forbidden = [name], _FORBIDDEN_CHARS
    for segment in segments:
        if not segment:
            raise ValueError(f"{kind} cannot contain an empty segment (got {name!r})")
        if segment in (".", ".."):
            raise ValueError(f"{kind} cannot contain '.' or '..' (got {name!r})")
        if segment.startswith("."):
            raise ValueError(f"{kind} cannot start with '.' (got {name!r})")
        for ch in forbidden:
            if ch in segment:
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
    )


# ---------------------------------------------------------------------------
# Per-service artifact indexer
# ---------------------------------------------------------------------------


def _document(row) -> "engine.Document":
    from awm.persistence import embeddings as engine
    body = "\n".join(x for x in (row["description"] or "",
                                  f"Type: {row['artifact_type'] or ''}",
                                  f"Path: {row['path'] or ''}",
                                  f"Tags: {row['tags'] or ''}") if x)
    return engine.Document(str(row["id"]), body, title=row["name"] or "",
                           context=f"{row['project']}/{row['scope']}")


def _index_artifact(artifact_id: int) -> None:
    """Index one artifact in this service's search index; logs and moves on on failure.

    A missed write is repaired by the next :func:`reindex_artifacts`, which the
    service runs at startup and on every ``sync``.
    """
    from awm.persistence import embeddings as engine
    from awm.persistence.databases import get_connection

    row = ArtifactsDAO().get_by_id(artifact_id)
    if not row:
        return
    conn = get_connection("artifacts")
    try:
        engine.index_document(conn, "artifact", _document(row))
    except Exception:  # noqa: BLE001 — registering must not fail on the index
        log.warning("artifacts: indexing %s failed", artifact_id, exc_info=True)
    finally:
        conn.close()


def reindex_artifacts(*, force: bool = False, dry_run: bool = False) -> dict:
    """Bring the search index in line with the current artifacts; returns counts."""
    from awm.persistence import embeddings as engine
    from awm.persistence.databases import get_connection

    conn = get_connection("artifacts")
    try:
        rows = conn.execute(
            "SELECT id, project, scope, name, artifact_type, path, description, tags"
            " FROM artifacts WHERE status='current'").fetchall()
        return engine.reindex(conn, "artifact", [_document(r) for r in rows],
                              force=force, prune=True, dry_run=dry_run)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Public API — register
# ---------------------------------------------------------------------------


def register_artifact(req: ArtifactRegisterRequest) -> ArtifactInfo:
    """Upserts on (path, project) — validate (project, scope) via scopes RPC
    BEFORE writing. On unresolvable scope, fail LOUDLY — no orphan rows."""
    _validate_name(req.project, kind="project name")
    _validate_name(req.scope, kind="scope name", allow_nesting=True)

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
        from awm.persistence.embeddings import delete_document
        delete_document(conn, "artifact", str(aid))

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
    """Search/filter current artifacts.

    With a ``query``, name/description/tag substring matches come first, then
    the rest ranked by meaning and keywords. Filters apply before ranking.
    """
    dao = ArtifactsDAO()
    rows = dao.search(
        project=project,
        scope=scope,
        artifact_type=artifact_type,
        query=query,
        limit=limit,
        offset=offset,
    )
    merged = [_row_to_info(r) for r in rows]
    if not query:
        return ArtifactSearchResponse(artifacts=merged, total=len(merged))

    where, params = "", []
    for col, val in (("project", project), ("scope", scope), ("artifact_type", artifact_type)):
        if val:
            where += f" AND {col} = ?"
            params.append(val)
    seen = {i.id for i in merged}
    degraded = None
    try:
        from awm.persistence import embeddings as engine
        from awm.persistence.databases import get_connection
        conn = get_connection("artifacts")
        try:
            res = engine.search(
                conn, query, source_type="artifact", params=params, limit=offset + limit,
                allowed="SELECT CAST(id AS TEXT) FROM artifacts WHERE status='current'" + where)
        finally:
            conn.close()
        degraded = res.degraded
        for h in res.hits[offset:]:
            if len(merged) >= limit:
                break
            row = dao.get_by_id(int(h["source_id"]))
            if row is not None and row["id"] not in seen:
                merged.append(_row_to_info(row))
                seen.add(row["id"])
    except Exception as exc:  # noqa: BLE001 — reported, then answered by substring match
        degraded = {"semantic": "error", "error": repr(exc)[:300], "fallback": "keyword"}
    return ArtifactSearchResponse(artifacts=merged, total=len(merged), degraded=degraded)


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
# Sync — flip artifact status based on on-disk presence, then reindex
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

    with dao.transaction() as conn:
        rows = dao.get_all_for_sync(conn=conn)

        for r in rows:
            aid, rel_path, status = r["id"], r["path"], r["status"]
            exists = (WORKSPACE_ROOT / rel_path).exists()
            if status == "current" and not exists:
                marked_stale.append(aid)
            elif status == "stale" and exists:
                restored.append(aid)

        ts = now_ms()
        if marked_stale:
            dao.mark_stale(marked_stale, ts, conn=conn)
        if restored:
            dao.restore_current(restored, ts, conn=conn)

    index = reindex_artifacts()

    set_config(_SYNC_FP_KEY, dao.get_fingerprint())
    return {
        "skipped": False,
        "marked_stale": len(marked_stale),
        "restored": len(restored),
        "indexed": index["indexed"],
        "embeddings_pruned": index["pruned"],
    }
