"""Lock acquire/release/heartbeat/reap with file + folder semantics.

Phase 7 — cross-peer routing. Locks themselves stay local-only (NOT
CRR-replicated): two peers cannot share an authoritative lock without
breaking the semantics. But a resource that lives on peer X (a scope
created on X, an artifact registered on X, a room hosted on X) should
have its locks held on X — otherwise locking a remote-origin resource
locally only blocks the local agent, not the actual owner.

So acquire/release/heartbeat parse the ``resource_path`` against a small
grammar:

  scope:<project>/<name>[@<peer>]
  artifact:<legacy_id>[@<peer>]
  room:<legacy_id>[@<peer>]

…look up the resource's ``origin_peer`` in the DB, and if it's a remote
peer, forward the operation to that peer's ``POST /peer/lock/...``
endpoint. Anything that doesn't match the grammar is treated as a local-
only resource (existing behavior).
"""

from __future__ import annotations

import logging
import os
import re
import sqlite3
from datetime import datetime, timezone

from awm.db import get_connection
from awm.config import HEARTBEAT_STALE_THRESHOLD
from awm.models import (
    LockAcquireRequest,
    LockInfo,
    LockListResponse,
    LockActionResponse,
)
from awm.services.network import peers as peer_svc

log = logging.getLogger("awm.services.locks")


# Resource-path grammar for federation. Matches:
#   scope:proj/name        scope:proj/name@peer
#   artifact:<legacy>      artifact:<legacy>@peer
#   room:<legacy>          room:<legacy>@peer
_RESOURCE_RE = re.compile(
    r"^(?P<kind>scope|artifact|room):"
    r"(?P<body>[^@]+?)"
    r"(?:@(?P<peer>[A-Za-z0-9][A-Za-z0-9_-]{0,63}))?$"
)


def _local_peer_id() -> str:
    try:
        ident = peer_svc.get_local_identity()
    except Exception:
        return ""
    if ident is None:
        return ""
    return ident.get("peer_id") or ""


def _resolve_owner(resource_path: str) -> str | None:
    """Return the peer id that owns ``resource_path``, or None if the path
    is local-only / unstructured.

    A returned peer id may equal the local peer, in which case the caller
    handles the lock locally (no federation hop). Returning None means the
    path doesn't fit the federated-resource grammar — local-only.
    """
    m = _RESOURCE_RE.match(resource_path)
    if m is None:
        return None
    kind = m.group("kind")
    body = m.group("body")
    suffix_peer = m.group("peer")
    if suffix_peer:
        return suffix_peer

    conn = get_connection()
    try:
        if kind == "scope":
            if "/" not in body:
                return None
            project, scope_name = body.split("/", 1)
            row = conn.execute(
                "SELECT origin_peer FROM scopes "
                "WHERE project = ? AND scope = ? "
                "ORDER BY updated_at DESC LIMIT 1",
                (project, scope_name),
            ).fetchone()
        elif kind == "artifact":
            try:
                legacy = int(body)
            except ValueError:
                return None
            row = conn.execute(
                "SELECT origin_peer FROM artifacts WHERE legacy_id = ? "
                "ORDER BY updated_at DESC LIMIT 1",
                (legacy,),
            ).fetchone()
        elif kind == "room":
            row = conn.execute(
                "SELECT host_peer_id AS origin_peer FROM rooms "
                "WHERE id = ? LIMIT 1",
                (body,),
            ).fetchone()
        else:
            return None
    finally:
        conn.close()

    if row is None:
        return None
    owner = row["origin_peer"] or ""
    return owner or None


# Peers we've ever federated a lock to in this process — used to fan out
# heartbeats. In-memory only; on restart the next acquire repopulates.
_federated_peers: set[str] = set()


def _row_to_info(row: sqlite3.Row) -> LockInfo:
    return LockInfo(
        id=row["id"],
        resource_path=row["resource_path"],
        holder_id=row["holder_id"],
        holder_pid=row["holder_pid"],
        lock_type=row["lock_type"],
        acquired_at=row["acquired_at"],
        heartbeat_at=row["heartbeat_at"],
        metadata=row["metadata"],
    )


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _pid_alive(pid: int) -> bool:
    """Check if a PID is alive (Linux /proc check)."""
    return os.path.isdir(f"/proc/{pid}")


def _find_conflicts(
    conn: sqlite3.Connection,
    resource_path: str,
    holder_id: str,
    lock_type: str,
) -> list[LockInfo]:
    """Find existing locks that conflict with a requested lock."""
    rows = conn.execute(
        """
        SELECT * FROM locks WHERE
            (resource_path = ?1) OR
            (?1 LIKE resource_path || '%') OR
            (resource_path LIKE ?1 || '%')
        """,
        (resource_path,),
    ).fetchall()

    conflicts = []
    for row in rows:
        # Same holder on same path — idempotent re-acquire
        if row["holder_id"] == holder_id and row["resource_path"] == resource_path:
            continue
        # Two shared locks on the exact same path are fine
        if lock_type == "shared" and row["lock_type"] == "shared" and row["resource_path"] == resource_path:
            continue
        # Everything else is a conflict
        conflicts.append(_row_to_info(row))
    return conflicts


def acquire(req: LockAcquireRequest) -> LockActionResponse:
    """Acquire a lock, federating to the owning peer when the resource_path
    matches the federated grammar and the owner is remote."""
    owner = _resolve_owner(req.resource_path)
    local = _local_peer_id()
    if owner is not None and owner != local and owner != "":
        from awm.services.network import federation
        try:
            resp = federation.forward_lock(owner, "acquire", req.model_dump())
        except federation.FederationError as exc:
            return LockActionResponse(
                message=f"federation_unreachable: {exc}", lock=None,
            )
        _federated_peers.add(owner)
        return _coerce_lock_action_response(resp)

    conn = get_connection()
    try:
        conflicts = _find_conflicts(conn, req.resource_path, req.holder_id, req.lock_type)
        if conflicts:
            holders = ", ".join(f"{c.holder_id} ({c.resource_path})" for c in conflicts)
            return LockActionResponse(
                message=f"Conflict: resource locked by {holders}",
                lock=None,
            )

        now = _now_iso()
        # Upsert — if same holder re-acquires, update heartbeat
        conn.execute(
            """
            INSERT INTO locks (resource_path, holder_id, holder_pid, lock_type, acquired_at, heartbeat_at, metadata)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(resource_path, holder_id) DO UPDATE SET
                heartbeat_at = excluded.heartbeat_at,
                holder_pid = excluded.holder_pid,
                lock_type = excluded.lock_type,
                metadata = excluded.metadata
            """,
            (req.resource_path, req.holder_id, req.holder_pid, req.lock_type, now, now, req.metadata),
        )
        conn.commit()

        row = conn.execute(
            "SELECT * FROM locks WHERE resource_path = ? AND holder_id = ?",
            (req.resource_path, req.holder_id),
        ).fetchone()

        info = _row_to_info(row)
        try:
            from awm.services.embeddings import index_lock
            index_lock(row["id"])
        except Exception:
            pass

        return LockActionResponse(
            message="Lock acquired",
            lock=info,
        )
    finally:
        conn.close()


def release(resource_path: str, holder_id: str) -> LockActionResponse:
    """Release a specific lock, federating when the resource is remote-owned."""
    owner = _resolve_owner(resource_path)
    local = _local_peer_id()
    if owner is not None and owner != local and owner != "":
        from awm.services.network import federation
        try:
            resp = federation.forward_lock(
                owner, "release",
                {"resource_path": resource_path, "holder_id": holder_id},
            )
        except federation.FederationError as exc:
            return LockActionResponse(
                message=f"federation_unreachable: {exc}", lock=None,
            )
        return _coerce_lock_action_response(resp)
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM locks WHERE resource_path = ? AND holder_id = ?",
            (resource_path, holder_id),
        ).fetchone()
        if row is None:
            return LockActionResponse(message="No matching lock found")

        info = _row_to_info(row)
        lock_row_id = row["id"]
        conn.execute(
            "DELETE FROM locks WHERE resource_path = ? AND holder_id = ?",
            (resource_path, holder_id),
        )
        conn.commit()
        try:
            from awm.services.embeddings import delete_embedding
            delete_embedding("lock", str(lock_row_id))
        except Exception:
            pass
        return LockActionResponse(message="Lock released", lock=info)
    finally:
        conn.close()


def heartbeat(holder_id: str) -> LockActionResponse:
    """Renew heartbeat for all locks held by a given holder. Heartbeat is
    fanned out to every peer this process has ever federated to in
    addition to the local update."""
    conn = get_connection()
    try:
        now = _now_iso()
        cursor = conn.execute(
            "UPDATE locks SET heartbeat_at = ? WHERE holder_id = ?",
            (now, holder_id),
        )
        conn.commit()
        local_count = cursor.rowcount
    finally:
        conn.close()

    remote_count = 0
    if _federated_peers:
        from awm.services.network import federation
        for peer in list(_federated_peers):
            try:
                resp = federation.forward_lock(
                    peer, "heartbeat", {"holder_id": holder_id},
                )
                if isinstance(resp, dict):
                    msg = resp.get("message", "")
                    # Best-effort: extract the count from "Heartbeat renewed for N lock(s)"
                    m = re.search(r"for (\d+) lock", msg)
                    if m:
                        remote_count += int(m.group(1))
            except federation.FederationError as exc:
                log.warning("heartbeat to %s failed: %s", peer, exc)

    return LockActionResponse(
        message=f"Heartbeat renewed for {local_count + remote_count} lock(s)",
    )


def _coerce_lock_action_response(payload) -> LockActionResponse:
    """Pydantic-revive a remote /peer/lock/* JSON response, tolerating that
    ``lock`` may be None or a nested dict."""
    if isinstance(payload, LockActionResponse):
        return payload
    if not isinstance(payload, dict):
        return LockActionResponse(message=str(payload), lock=None)
    lock_payload = payload.get("lock")
    lock_info = None
    if isinstance(lock_payload, dict):
        try:
            lock_info = LockInfo(**lock_payload)
        except Exception:  # noqa: BLE001
            lock_info = None
    return LockActionResponse(
        message=payload.get("message", ""), lock=lock_info,
    )


def search_locks(
    query: str | None = None,
    status: str = "active",
    holder_id: str | None = None,
    path: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> LockListResponse:
    """Search locks. Defaults to status='active' (live PID + fresh heartbeat).
    Pass status='stale' to see reapable rows or status='all' for the
    firehose. `query` LIKEs on resource_path and holder_id; semantic top-up
    uses the embeddings table."""
    sql = "SELECT * FROM locks WHERE 1=1"
    params: list = []
    if holder_id:
        sql += " AND holder_id = ?"
        params.append(holder_id)
    if path:
        sql += " AND resource_path = ?"
        params.append(path)
    if query:
        sql += " AND (resource_path LIKE ? OR holder_id LIKE ?)"
        like = f"%{query}%"
        params.extend([like, like])
    sql += " ORDER BY acquired_at LIMIT ? OFFSET ?"
    params.extend([limit, offset])

    conn = get_connection()
    try:
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()

    if status == "active":
        rows = [r for r in rows if not _is_stale(r)]
    elif status == "stale":
        rows = [r for r in rows if _is_stale(r)]
    # status == "all" → no filtering

    locks = [_row_to_info(r) for r in rows]

    if not query:
        return LockListResponse(locks=locks, total=len(locks))

    keyword_keys = {str(l.id) for l in locks if hasattr(l, "id") and l.id is not None}

    def _materialize(lock_id: str):
        try:
            lid = int(lock_id)
        except ValueError:
            return None
        conn = get_connection()
        try:
            row = conn.execute("SELECT * FROM locks WHERE id = ?", (lid,)).fetchone()
        finally:
            conn.close()
        if row is None:
            return None
        if status == "active" and _is_stale(row):
            return None
        if status == "stale" and not _is_stale(row):
            return None
        if holder_id and row["holder_id"] != holder_id:
            return None
        if path and row["resource_path"] != path:
            return None
        return _row_to_info(row)

    try:
        from awm.services.embeddings import hybrid_augment
        merged = hybrid_augment(
            query, source_type="lock",
            keyword_hits=locks, keyword_keys=keyword_keys,
            materialize=_materialize,
        )
    except Exception:
        merged = locks
    return LockListResponse(locks=merged, total=len(merged))


def _is_stale(row, *, now: datetime | None = None) -> bool:
    """Return True if a locks row is stale: holder PID gone, or heartbeat
    older than HEARTBEAT_STALE_THRESHOLD. Lifted from `reap_stale` so the
    read path (`search_locks(status='active')`) and the reaper can share
    the same staleness rule."""
    if row["holder_pid"] and not _pid_alive(row["holder_pid"]):
        return True
    hb = datetime.fromisoformat(row["heartbeat_at"])
    if hb.tzinfo is None:
        hb = hb.replace(tzinfo=timezone.utc)
    now = now or datetime.now(timezone.utc)
    return (now - hb).total_seconds() > HEARTBEAT_STALE_THRESHOLD


def reap_stale() -> int:
    """Remove stale locks. Returns count of reaped locks."""
    conn = get_connection()
    try:
        now = datetime.now(timezone.utc)
        rows = conn.execute("SELECT * FROM locks").fetchall()
        reaped_ids: list[int] = []
        for row in rows:
            if _is_stale(row, now=now):
                conn.execute("DELETE FROM locks WHERE id = ?", (row["id"],))
                reaped_ids.append(row["id"])

        conn.commit()
    finally:
        conn.close()

    try:
        from awm.services.embeddings import delete_embedding
        for lid in reaped_ids:
            delete_embedding("lock", str(lid))
    except Exception:
        pass

    return len(reaped_ids)
