"""REST + WebSocket router for the rooms surface.

Mounted from ``awm/exposed.py``. Bearer auth applies via the
``require_bearer`` dependency on REST routes; the WS attach endpoint
uses :func:`awm.ws_auth.authenticate_room_ws`.

Cross-peer plumbing (M3) is integrated via ``RemoteRoomProxy`` and the
forward_room_* helpers in ``awm.services.network.federation``.
"""

from __future__ import annotations

import asyncio
import json
from typing import Optional

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Query,
    Request,
    WebSocket,
    WebSocketDisconnect,
)

from awm import ws_envelope as env
from awm.middleware_auth import require_bearer
from awm.models import (
    ParticipantInfo,
    PostInfo,
    RoomActionResponse,
    RoomCloseRequest,
    RoomCreateRequest,
    RoomDetail,
    RoomHistoryResponse,
    RoomInfo,
    RoomInviteRequest,
    RoomListResponse,
    RoomPostRequest,
    RoomRemoveRequest,
)
from awm.services import orchestration, rooms as rooms_svc
from awm.ws_auth import authenticate_room_ws


router = APIRouter(prefix="/rooms", tags=["rooms"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _room_info(room: rooms_svc.Room) -> RoomInfo:
    return RoomInfo(**room.to_dict())


def _post_info(post: rooms_svc.Post) -> PostInfo:
    return PostInfo(**post.to_dict())


def _participant_info(p: rooms_svc.Participant) -> ParticipantInfo:
    return ParticipantInfo(**p.to_dict())


def _opener_from_request(request: Request) -> str:
    """Resolve the post author from request headers — X-Awm-From peer claim
    and X-Awm-As user claim. Defaults to ``user:operator``."""
    from_peer = getattr(request.state, "from_peer", None) or \
        request.headers.get("x-awm-from")
    user_as = request.headers.get("x-awm-as") or "user:operator"
    if from_peer and "@" not in user_as:
        user_as = f"{user_as}@{from_peer}"
    return user_as


# ---------------------------------------------------------------------------
# REST
# ---------------------------------------------------------------------------

@router.post("", response_model=RoomInfo, dependencies=[Depends(require_bearer)])
async def create_room(req: RoomCreateRequest, request: Request) -> RoomInfo:
    opener = _opener_from_request(request)
    try:
        room = await orchestration.create_room_with_scopes(
            topic=req.topic, scopes=req.scopes, prompts=req.prompts,
            opener=opener, close_on_exit=req.close_on_exit,
        )
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))
    except RuntimeError as e:
        raise HTTPException(500, str(e))
    except rooms_svc.RoomError as e:
        raise HTTPException(409, str(e))
    return _room_info(room)


@router.get("", response_model=RoomListResponse, dependencies=[Depends(require_bearer)])
def list_rooms(
    status: str = Query("active"),
    participating_scope: str | None = Query(None),
    peer: str | None = Query(None),
    limit: int = Query(100, ge=1, le=500),
):
    if peer and peer != "local":
        # M3 cross-peer fan-out.
        from awm.services.network import federation
        if peer == "all":
            from awm.services.network import peers as peer_svc
            peer_ids = [p["peer_id"] for p in peer_svc.list_peers()]
        else:
            peer_ids = [peer]
        # Local rooms.
        local = [_room_info(r).model_dump() for r in
                 rooms_svc.list_rooms(status=status,
                                      participating_scope=participating_scope,
                                      limit=limit)]
        merged = list(local)
        for pid in peer_ids:
            try:
                remote = federation.forward_room_list(
                    pid, status=status,
                    participating_scope=participating_scope, limit=limit,
                )
                merged.extend(remote.get("rooms", []))
            except federation.FederationError:
                continue
        return RoomListResponse(rooms=merged, total=len(merged))
    rooms = rooms_svc.list_rooms(
        status=status, participating_scope=participating_scope, limit=limit,
    )
    return RoomListResponse(rooms=[_room_info(r) for r in rooms], total=len(rooms))


@router.get("/search", dependencies=[Depends(require_bearer)])
def search_rooms(
    q: str = Query(..., min_length=1),
    peer: str | None = Query(None),
    limit: int = Query(20, ge=1, le=100),
):
    local = [_room_info(r).model_dump() for r in rooms_svc.search_rooms(q, limit=limit)]
    degraded: list[dict] = []
    if peer and peer != "local":
        from awm.services.network import federation, peers as peer_svc
        if peer == "all":
            peer_ids = [p["peer_id"] for p in peer_svc.list_peers()]
        else:
            peer_ids = [peer]
        for pid in peer_ids:
            try:
                remote = federation.forward_room_search(pid, q, limit=limit)
                local.extend(remote.get("rooms", []))
            except federation.FederationError as exc:
                degraded.append({"peer_id": pid, "reason": str(exc)})
    return {"rooms": local, "total": len(local), "degraded": degraded}


@router.get("/{room_id}", response_model=RoomDetail, dependencies=[Depends(require_bearer)])
def get_room(room_id: str):
    room = rooms_svc.get_room(room_id)
    if room is None:
        raise HTTPException(404, f"no such room: {room_id}")
    participants = rooms_svc.list_participants(room_id, active_only=False)
    recent = rooms_svc.history(room_id, limit_chars=1024)
    return RoomDetail(
        room=_room_info(room),
        participants=[_participant_info(p) for p in participants],
        recent=[_post_info(p) for p in recent],
    )


@router.get("/{room_id}/history", response_model=RoomHistoryResponse,
            dependencies=[Depends(require_bearer)])
def get_history(
    room_id: str,
    before_ts: Optional[str] = Query(None),
    limit_chars: int = Query(4096, ge=128, le=131072),
):
    try:
        posts = rooms_svc.history(room_id, limit_chars=limit_chars, before_ts=before_ts)
    except rooms_svc.RoomNotFound:
        raise HTTPException(404, f"no such room: {room_id}")
    return RoomHistoryResponse(
        posts=[_post_info(p) for p in posts], total=len(posts),
    )


@router.post("/{room_id}/posts", response_model=RoomActionResponse,
             dependencies=[Depends(require_bearer)])
def post_to_room(room_id: str, req: RoomPostRequest, request: Request):
    author = _opener_from_request(request)
    try:
        post = rooms_svc.post(room_id, author=author, body=req.body,
                              kind=req.kind, to_scope=req.to)
    except rooms_svc.RoomNotFound:
        raise HTTPException(404, f"no such room: {room_id}")
    except rooms_svc.RoomClosed:
        raise HTTPException(409, f"room {room_id} is closed")
    return RoomActionResponse(message="posted", post=_post_info(post))


@router.post("/{room_id}/invite", response_model=RoomActionResponse,
             dependencies=[Depends(require_bearer)])
async def invite_to_room(room_id: str, req: RoomInviteRequest, request: Request):
    opener = _opener_from_request(request)
    try:
        participant = await orchestration.invite_scope_to_room(
            room_id, req.scope, prompt=req.prompt, opener=opener,
        )
    except rooms_svc.RoomNotFound:
        raise HTTPException(404, f"no such room: {room_id}")
    except rooms_svc.RoomClosed:
        raise HTTPException(409, f"room {room_id} is closed")
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc))
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except rooms_svc.ScopeBusyError as exc:
        # The scope's session is already running — joining it to the room
        # is still legal. Re-fetch the participant.
        participant = rooms_svc.add_participant(room_id, "scope", req.scope)
    return RoomActionResponse(
        message="invited",
        participant=_participant_info(participant),
    )


@router.post("/{room_id}/remove", response_model=RoomActionResponse,
             dependencies=[Depends(require_bearer)])
def remove_from_room(room_id: str, req: RoomRemoveRequest):
    ok = rooms_svc.remove_participant(room_id, "scope", req.scope)
    if not ok:
        raise HTTPException(404, "participant not found or already left")
    return RoomActionResponse(message="removed")


# ---------------------------------------------------------------------------
# Cross-peer (M3) receiving endpoints — invoked by federation.forward_*
# from another peer over an SSH tunnel.
# ---------------------------------------------------------------------------

@router.post("/{room_id}/shadow-join", dependencies=[Depends(require_bearer)])
def shadow_join(room_id: str, request: Request):
    """A remote peer is announcing itself as a shadow_peer for this room.
    The remote peer is identified by ``X-Awm-From`` (validated by the
    exposed middleware to be a known peer)."""
    from_peer = request.headers.get("x-awm-from")
    if not from_peer:
        raise HTTPException(400, "X-Awm-From required")
    try:
        participant = rooms_svc.add_participant(room_id, "shadow_peer", from_peer)
    except rooms_svc.RoomNotFound:
        raise HTTPException(404, f"no such room: {room_id}")
    except rooms_svc.RoomClosed:
        raise HTTPException(409, f"room {room_id} is closed")
    return {"ok": True, "participant": participant.to_dict()}


@router.post("/{room_id}/shadow-leave", dependencies=[Depends(require_bearer)])
def shadow_leave(room_id: str, request: Request):
    from_peer = request.headers.get("x-awm-from")
    if not from_peer:
        raise HTTPException(400, "X-Awm-From required")
    rooms_svc.remove_participant(room_id, "shadow_peer", from_peer)
    return {"ok": True}


@router.post("/internal/agent-input", dependencies=[Depends(require_bearer)])
def receive_agent_input(request: Request, body: dict):
    """Remote peer is pushing an input frame for a scope that lives here."""
    from_peer = request.headers.get("x-awm-from")
    if not from_peer:
        raise HTTPException(400, "X-Awm-From required")
    scope = body.get("scope")
    room_id = body.get("room_id")
    text = body.get("body", "")
    author = body.get("author", f"user:operator@{from_peer}")
    if not scope or not room_id:
        raise HTTPException(400, "scope and room_id required")

    # Synthesize an input frame for the local LiveSession.
    from awm.services import sessions_live
    session = sessions_live._by_scope.get(scope)
    if session is None:
        # Spawn-on-demand for cross-host agent inputs.
        try:
            project, scope_name = scope.split("/", 1)
        except ValueError:
            raise HTTPException(400, f"invalid scope: {scope}")

        import asyncio
        async def _spawn():
            return await sessions_live.create_session(
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
    sessions_live.enqueue_input(session, room_id, fake_post)
    return {"ok": True}


@router.post("/{room_id}/close", response_model=RoomActionResponse,
             dependencies=[Depends(require_bearer)])
def close_room(room_id: str, req: RoomCloseRequest):
    try:
        room = rooms_svc.close_room(room_id, kill_agents=req.kill_agents)
    except rooms_svc.RoomNotFound:
        raise HTTPException(404, f"no such room: {room_id}")
    return RoomActionResponse(message="closed", room=_room_info(room))


# ---------------------------------------------------------------------------
# WebSocket attach
# ---------------------------------------------------------------------------

_WS_QUEUE_MAX = 256


@router.websocket("/{room_id}/attach")
async def attach_ws(websocket: WebSocket, room_id: str):
    auth = await authenticate_room_ws(websocket)
    if not auth.ok:
        return
    await websocket.accept(subprotocol=auth.subprotocol)

    room = rooms_svc.get_room(room_id)
    if room is None:
        await websocket.send_text(json.dumps(env.error(f"no such room: {room_id}")))
        await websocket.close(code=1008, reason="no such room")
        return

    queue: asyncio.Queue = asyncio.Queue(maxsize=_WS_QUEUE_MAX)
    subscriber_id = f"ws:{id(websocket)}:{auth.user_as}"
    await rooms_svc.subscribe_room(room_id, queue)
    try:
        rooms_svc.add_participant(room_id, "subscriber", subscriber_id)
    except rooms_svc.RoomClosed:
        await websocket.send_text(json.dumps(env.error(f"room {room_id} is closed")))
        await websocket.close(code=1008, reason="room closed")
        await rooms_svc.unsubscribe_room(room_id, queue)
        return

    # Send transcript backlog.
    backlog = rooms_svc.history(room_id, limit_chars=4096)
    await websocket.send_text(json.dumps(
        env.history([_post_info(p).model_dump() for p in backlog])
    ))

    async def writer():
        while True:
            ev = await queue.get()
            if env.is_lagged(ev):
                try:
                    await websocket.send_text(json.dumps(ev))
                except Exception:
                    return
                # Drop the connection — caller should reconnect.
                await websocket.close(code=1011, reason="lagged")
                return
            try:
                await websocket.send_text(json.dumps(ev))
            except Exception:
                return

    async def reader():
        while True:
            try:
                raw = await websocket.receive_text()
            except WebSocketDisconnect:
                return
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                await websocket.send_text(json.dumps(env.error("invalid JSON")))
                continue
            mtype = msg.get("type")
            if mtype == "post":
                body = msg.get("body", "")
                kind = msg.get("kind", "text")
                to_scope = msg.get("to") or None
                try:
                    rooms_svc.post(room_id, author=auth.user_as, body=body,
                                   kind=kind, to_scope=to_scope)
                except rooms_svc.RoomError as exc:
                    await websocket.send_text(json.dumps(env.error(str(exc))))
            elif mtype == "control":
                action = msg.get("action")
                if action == "close":
                    try:
                        rooms_svc.close_room(room_id)
                    except rooms_svc.RoomError as exc:
                        await websocket.send_text(json.dumps(env.error(str(exc))))
                elif action == "kill":
                    try:
                        rooms_svc.close_room(room_id, kill_agents=True)
                    except rooms_svc.RoomError as exc:
                        await websocket.send_text(json.dumps(env.error(str(exc))))
                else:
                    await websocket.send_text(json.dumps(
                        env.error(f"unknown control action: {action}")))
            elif mtype == "ping":
                await websocket.send_text(json.dumps(env.pong()))
            else:
                await websocket.send_text(json.dumps(
                    env.error(f"unknown envelope type: {mtype}")))

    writer_task = asyncio.create_task(writer())
    reader_task = asyncio.create_task(reader())
    try:
        done, _pending = await asyncio.wait(
            {writer_task, reader_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
    finally:
        writer_task.cancel()
        reader_task.cancel()
        await rooms_svc.unsubscribe_room(room_id, queue)
        rooms_svc.remove_participant(room_id, "subscriber", subscriber_id)
        try:
            if websocket.client_state.name != "DISCONNECTED":
                await websocket.close()
        except Exception:
            pass
