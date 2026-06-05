"""Peer-facing API surface.

Mounted on the daemon under ``/peer/...``. Every route here is called by
*another* peer (over its peer-token bearer + ``X-Awm-From`` origin claim),
never by an operator. Routes intentionally do not appear in the CLI or
MCP catalogs: the peer client (``awm.services.network.federation`` for
now, soon renamed ``peer_client``) is the only legitimate caller.

The split between this router and ``awm/api/rooms.py`` /
``awm/server.py:/inbox`` is the architectural boundary described in
``161-and-160-are-agile-scroll.md`` Phase 6 — user-facing routes never
consult ``X-Awm-From``; peer-facing routes always do.
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException, Request

from awm.services.auth.middleware_auth import require_peer_bearer
from awm.services import rooms as rooms_svc


router = APIRouter(prefix="/peer", tags=["peer"])


def _require_from_peer(request: Request) -> str:
    """Return the origin peer id, 400 if missing."""
    from_peer = getattr(request.state, "from_peer", None) \
        or request.headers.get("x-awm-from")
    if not from_peer:
        raise HTTPException(400, "X-Awm-From required")
    return from_peer


# ---------------------------------------------------------------------------
# Federated inbox
# ---------------------------------------------------------------------------

@router.post("/inbox", dependencies=[Depends(require_peer_bearer)])
def peer_inbox(request: Request, payload: dict):
    """Receive a federated message from a remote peer.

    The remote awm forwards a ``MessageSendRequest`` body here; the local
    inbox stores it after rewriting the sender to include the origin
    peer_id (extracted from ``X-Awm-From``).
    """
    from awm.models import MessageSendRequest
    from awm.services import messaging

    origin_peer = _require_from_peer(request)
    body = dict(payload)
    original_sender = body.get("sender", "unknown")
    body["sender"] = f"peer:{origin_peer}/{original_sender}"

    try:
        req = MessageSendRequest(**body)
    except Exception as exc:
        raise HTTPException(400, f"invalid message body: {exc}")
    try:
        return messaging.send_message(req)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


# ---------------------------------------------------------------------------
# Federated room post (the cross-peer broadcast receiver)
# ---------------------------------------------------------------------------

@router.post("/rooms/{room_id}/posts", dependencies=[Depends(require_peer_bearer)])
def peer_room_post(room_id: str, request: Request, payload: dict):
    """Receive a post from a remote peer.

    The author is rewritten to ``user:operator@<from_peer>`` so the
    transcript on this host stays unambiguous.
    """
    from_peer = _require_from_peer(request)
    user_as = request.headers.get("x-awm-as") or "user:operator"
    if "@" not in user_as:
        user_as = f"{user_as}@{from_peer}"
    body = payload.get("body", "")
    kind = payload.get("kind", "text")
    to_scope = payload.get("to")
    try:
        post = rooms_svc.post(room_id, author=user_as, body=body,
                              kind=kind, to_scope=to_scope)
    except rooms_svc.RoomNotFound:
        raise HTTPException(404, f"no such room: {room_id}")
    except rooms_svc.RoomClosed:
        raise HTTPException(409, f"room {room_id} is closed")
    return {"message": "posted", "post": post.to_dict()}


# ---------------------------------------------------------------------------
# Shadow participants
# ---------------------------------------------------------------------------

@router.post("/rooms/{room_id}/shadow-join", dependencies=[Depends(require_peer_bearer)])
def peer_shadow_join(room_id: str, request: Request):
    """A remote peer is announcing itself as a ``shadow_peer`` participant."""
    from_peer = _require_from_peer(request)
    try:
        participant = rooms_svc.add_participant(room_id, "shadow_peer", from_peer)
    except rooms_svc.RoomNotFound:
        raise HTTPException(404, f"no such room: {room_id}")
    except rooms_svc.RoomClosed:
        raise HTTPException(409, f"room {room_id} is closed")
    return {"ok": True, "participant": participant.to_dict()}


@router.post("/rooms/{room_id}/shadow-leave", dependencies=[Depends(require_peer_bearer)])
def peer_shadow_leave(room_id: str, request: Request):
    from_peer = _require_from_peer(request)
    rooms_svc.remove_participant(room_id, "shadow_peer", from_peer)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Cross-host agent input
# ---------------------------------------------------------------------------

@router.post("/rooms/agent-input", dependencies=[Depends(require_peer_bearer)])
def peer_agent_input(request: Request, body: dict):
    """Remote peer is pushing an input frame for a scope that lives here."""
    from awm.services import agent_instances

    from_peer = _require_from_peer(request)
    scope = body.get("scope")
    room_id = body.get("room_id")
    text = body.get("body", "")
    author = body.get("author", f"user:operator@{from_peer}")
    if not scope or not room_id:
        raise HTTPException(400, "scope and room_id required")

    session = agent_instances._by_scope.get(scope)
    if session is None:
        try:
            project, scope_name = scope.split("/", 1)
        except ValueError:
            raise HTTPException(400, f"invalid scope: {scope}")

        async def _spawn():
            return await agent_instances.create_session(
                project=project, scope=scope_name,
                # Federated worker needs free MCP-tool access; mirrors
                # awm/api/vagrant.py and awm/services/orchestration.py.
                permission_mode="bypassPermissions",
            )
        try:
            session = asyncio.run(_spawn())
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(500, f"could not spawn session: {exc}")

    fake_post = rooms_svc.Post(
        id=0, room_id=room_id, author=author, body=text, kind="text",
        ts="(cross-peer)",
    )
    agent_instances.enqueue_input(session, room_id, fake_post)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Federated artifact content (phase 6)
# ---------------------------------------------------------------------------

@router.get(
    "/artifacts/{artifact_id}/content",
    dependencies=[Depends(require_peer_bearer)],
)
def peer_artifact_content(artifact_id: int, request: Request):
    """Stream the bytes of a local artifact for a remote peer.

    v37 dropped cr-sqlite replication of the artifacts table, so the
    legacy_id/origin_peer disambiguation is gone — ``artifact_id`` is the
    plain INTEGER PK of an artifacts row that lives on this peer."""
    from fastapi.responses import Response as _Response
    from awm.db import get_connection
    from awm.services import artifacts as artifacts_svc

    _require_from_peer(request)
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT path FROM artifacts WHERE id=?",
            (artifact_id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        raise HTTPException(404, f"no local artifact with id={artifact_id}")
    try:
        data = artifacts_svc._read_local_content(row["path"])
    except artifacts_svc.ArtifactNotFound as exc:
        raise HTTPException(404, str(exc))
    return _Response(content=data, media_type="application/octet-stream")


# ---------------------------------------------------------------------------
# Federated locks (phase 7)
# ---------------------------------------------------------------------------

@router.post("/lock/acquire", dependencies=[Depends(require_peer_bearer)])
def peer_lock_acquire(request: Request, payload: dict):
    """A remote peer is acquiring a lock on a resource that lives here.
    We never re-federate: the resource is presumed to live locally (the
    caller already resolved origin_peer to this peer)."""
    from awm.models import LockAcquireRequest
    from awm.services import locks as locks_svc

    _require_from_peer(request)
    try:
        req = LockAcquireRequest(**payload)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"invalid lock-acquire body: {exc}")
    # Local-only path: skip the resolver since federation already routed us.
    return _local_lock_acquire(req)


@router.post("/lock/release", dependencies=[Depends(require_peer_bearer)])
def peer_lock_release(request: Request, payload: dict):
    from awm.services import locks as locks_svc

    _require_from_peer(request)
    resource_path = payload.get("resource_path")
    holder_id = payload.get("holder_id")
    if not resource_path or not holder_id:
        raise HTTPException(400, "resource_path and holder_id required")
    return _local_lock_release(resource_path, holder_id)


@router.post("/lock/heartbeat", dependencies=[Depends(require_peer_bearer)])
def peer_lock_heartbeat(request: Request, payload: dict):
    from awm.services import locks as locks_svc

    _require_from_peer(request)
    holder_id = payload.get("holder_id")
    if not holder_id:
        raise HTTPException(400, "holder_id required")
    return _local_lock_heartbeat(holder_id)


def _local_lock_acquire(req):
    """Call the local-only acquire path, bypassing _resolve_owner. Used
    by the federated /peer/lock/acquire handler to prevent re-federation
    when a peer chain happens to misroute (e.g. stale origin_peer)."""
    from awm.services import locks as locks_svc

    conn = locks_svc.get_connection()
    try:
        conflicts = locks_svc._find_conflicts(
            conn, req.resource_path, req.holder_id, req.lock_type,
        )
        if conflicts:
            holders = ", ".join(
                f"{c.holder_id} ({c.resource_path})" for c in conflicts
            )
            return locks_svc.LockActionResponse(
                message=f"Conflict: resource locked by {holders}", lock=None,
            )
        now = locks_svc._now_iso()
        conn.execute(
            "INSERT INTO locks (resource_path, holder_id, holder_pid, "
            " lock_type, acquired_at, heartbeat_at, metadata) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(resource_path, holder_id) DO UPDATE SET "
            "  heartbeat_at = excluded.heartbeat_at, "
            "  holder_pid = excluded.holder_pid, "
            "  lock_type = excluded.lock_type, "
            "  metadata = excluded.metadata",
            (req.resource_path, req.holder_id, req.holder_pid,
             req.lock_type, now, now, req.metadata),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM locks WHERE resource_path = ? AND holder_id = ?",
            (req.resource_path, req.holder_id),
        ).fetchone()
        return locks_svc.LockActionResponse(
            message="Lock acquired", lock=locks_svc._row_to_info(row),
        )
    finally:
        conn.close()


def _local_lock_release(resource_path: str, holder_id: str):
    from awm.services import locks as locks_svc

    conn = locks_svc.get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM locks WHERE resource_path = ? AND holder_id = ?",
            (resource_path, holder_id),
        ).fetchone()
        if row is None:
            return locks_svc.LockActionResponse(message="No matching lock found")
        info = locks_svc._row_to_info(row)
        conn.execute(
            "DELETE FROM locks WHERE resource_path = ? AND holder_id = ?",
            (resource_path, holder_id),
        )
        conn.commit()
        return locks_svc.LockActionResponse(message="Lock released", lock=info)
    finally:
        conn.close()


def _local_lock_heartbeat(holder_id: str):
    from awm.services import locks as locks_svc

    conn = locks_svc.get_connection()
    try:
        now = locks_svc._now_iso()
        cur = conn.execute(
            "UPDATE locks SET heartbeat_at = ? WHERE holder_id = ?",
            (now, holder_id),
        )
        conn.commit()
        return locks_svc.LockActionResponse(
            message=f"Heartbeat renewed for {cur.rowcount} lock(s)",
        )
    finally:
        conn.close()
