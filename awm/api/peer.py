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

from awm.middleware_auth import require_bearer
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

@router.post("/inbox", dependencies=[Depends(require_bearer)])
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

@router.post("/rooms/{room_id}/posts", dependencies=[Depends(require_bearer)])
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

@router.post("/rooms/{room_id}/shadow-join", dependencies=[Depends(require_bearer)])
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


@router.post("/rooms/{room_id}/shadow-leave", dependencies=[Depends(require_bearer)])
def peer_shadow_leave(room_id: str, request: Request):
    from_peer = _require_from_peer(request)
    rooms_svc.remove_participant(room_id, "shadow_peer", from_peer)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Cross-host agent input
# ---------------------------------------------------------------------------

@router.post("/rooms/agent-input", dependencies=[Depends(require_bearer)])
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
