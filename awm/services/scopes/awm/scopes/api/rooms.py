"""REST + WebSocket router for the rooms surface (v1 modular).

Legacy agent service imports replaced:
  - ``orchestration.create_room_with_scopes`` → rooms_svc.create_room (inline)
  - ``orchestration.invite_scope_to_room`` → rooms_svc.add_participant (inline)
  - ``agent_slash.dispatch`` / ``agent_instances`` / ``agent_slash.server_catalog``
    → deferred: the agents service is out-of-process. Slash-command and
    live-agent-state endpoints currently return stubs or 501 until the
    agents service exposes the required RPCs:
      - ``agentSessionInfo(project, scope)`` — live PID/status/model state
      - ``sendSlash(project, scope, line)`` — forward slash to claude stdin
      - ``slashCatalog(project, scope)`` — server + claude slash commands
    These RPCs are NOT yet in the scopes RPC contract; see the BLOCKERS note
    below. Stub behaviour: GET /agents returns no live state, GET
    slash-commands returns an empty catalog, POST slash returns 501.

BLOCKERS (agents RPC not yet in manifest):
  The following routes partially stub because the agents service hasn't
  published the above RPCs. A follow-up integration pass wires them via
  gatewayclient.call('agents', ...) once agents exposes them.
"""

from __future__ import annotations

from typing import Optional

from fastapi import (
    APIRouter,
    HTTPException,
    Query,
    Request,
    WebSocket,
)

from awm.scopes.models import (
    ParticipantInfo,
    PostInfo,
    RoomActionResponse,
    RoomArchiveBlockedResponse,
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
from awm.scopes import rooms as rooms_svc


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
    return request.headers.get("x-awm-as") or "user:operator"


# ---------------------------------------------------------------------------
# REST
# ---------------------------------------------------------------------------

@router.post("", response_model=RoomInfo)
def create_room(req: RoomCreateRequest, request: Request) -> RoomInfo:
    opener = _opener_from_request(request)
    try:
        room = rooms_svc.create_room(
            topic=req.topic,
            scopes=req.scopes,
            opener=opener,
            close_on_exit=req.close_on_exit,
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


@router.get("/search")
def search_rooms(
    query: str | None = Query(None),
    status: str = Query("active"),
    participating_scope: str | None = Query(None),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    """Search rooms (hybrid keyword + semantic).
    Defaults to status='active'; pass status='all' for the full history."""
    local = [
        _room_info(r).model_dump()
        for r in rooms_svc.search_rooms(
            query,
            status=status, participating_scope=participating_scope,
            limit=limit, offset=offset,
        )
    ]
    return {"rooms": local, "total": len(local)}


@router.get("/{room_id}", response_model=RoomDetail)
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


@router.get("/{room_id}/history", response_model=RoomHistoryResponse)
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


@router.post("/{room_id}/posts", response_model=RoomActionResponse)
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


@router.post("/{room_id}/invite", response_model=RoomActionResponse)
def invite_to_room(room_id: str, req: RoomInviteRequest, request: Request):
    """Invite a scope to a room. In-process via rooms surface.

    NOTE: agent session spawn on invite is handled by the agents service
    (out-of-process). This endpoint performs the guest_list enrollment only.
    If the agents service should be notified, that integration is a follow-up
    once agents exposes an ``inviteToRoom(project, scope, room_id, prompt)`` RPC.
    """
    try:
        participant = rooms_svc.add_participant(room_id, "scope", req.scope)
    except rooms_svc.RoomNotFound:
        raise HTTPException(404, f"no such room: {room_id}")
    except rooms_svc.RoomClosed:
        raise HTTPException(409, f"room {room_id} is closed")
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc))
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except rooms_svc.ScopeBusyError:
        # Already registered
        participant = rooms_svc.add_participant(room_id, "scope", req.scope)
    return RoomActionResponse(
        message="invited",
        participant=_participant_info(participant),
    )


@router.post("/{room_id}/remove", response_model=RoomActionResponse)
def remove_from_room(room_id: str, req: RoomRemoveRequest):
    ok = rooms_svc.remove_participant(room_id, "scope", req.scope)
    if not ok:
        raise HTTPException(404, "participant not found or already left")
    return RoomActionResponse(message="removed")


@router.post("/{room_id}/close", response_model=RoomActionResponse)
def close_room(room_id: str, req: RoomCloseRequest):
    try:
        room = rooms_svc.close_room(room_id, kill_agents=req.kill_agents)
    except rooms_svc.RoomNotFound:
        raise HTTPException(404, f"no such room: {room_id}")
    return RoomActionResponse(message="closed", room=_room_info(room))


@router.post("/{room_id}/archive", response_model=RoomActionResponse,
             responses={409: {"model": RoomArchiveBlockedResponse}})
def archive_room(room_id: str):
    """Soft-archive a room. 409 with blocking_scopes if owner is still active."""
    try:
        room = rooms_svc.archive_room(room_id)
    except rooms_svc.RoomNotFound:
        raise HTTPException(404, f"no such room: {room_id}")
    except rooms_svc.RoomArchiveBlocked as exc:
        raise HTTPException(409, detail={
            "error": "room_archive_blocked",
            "room_id": exc.room_id,
            "blocking_scopes": exc.blocking_scopes,
        })
    return RoomActionResponse(message="archived", room=_room_info(room))


@router.get("/{room_id}/agents")
def get_room_agents(room_id: str):
    """List scope participants. Live agent state (pid/status/model) is NOT
    available yet: the agents service RPC for per-scope live state
    (``agentSessionInfo``) is not yet in the manifest. Returns participants
    with ``live=null`` until that integration is done."""
    room = rooms_svc.get_room(room_id)
    if room is None:
        raise HTTPException(404, f"no such room: {room_id}")

    agents = []
    for p in rooms_svc.list_participants(room_id, active_only=False):
        if p.kind not in ("scope", "shadow_peer"):
            continue
        agents.append({
            "scope": p.identifier,
            "kind": p.kind,
            "identifier": p.identifier,
            "joined_at": p.joined_at,
            "live": None,  # agents RPC not yet wired
        })
    return {"agents": agents}


# ---------------------------------------------------------------------------
# Slash-command surface (STUB — agents service RPC not yet published)
# ---------------------------------------------------------------------------

@router.get(
    "/{room_id}/agents/{scope:path}/slash-commands",
)
def get_agent_slash_commands(room_id: str, scope: str):
    """STUB: slash-command catalog requires agents RPC not yet published.
    Returns an empty catalog. Wire via gatewayclient.call('agents',
    'slashCatalog', {...}) once agents exposes that function."""
    _require_local_scope_participant(room_id, scope)
    return {"server": [], "claude": []}


@router.post(
    "/{room_id}/agents/{scope:path}/slash",
)
async def post_agent_slash(
    room_id: str, scope: str, request: Request,
):
    """STUB: slash-command dispatch requires agents RPC not yet published.
    Returns 501 until agents exposes sendSlash(project, scope, line)."""
    raise HTTPException(
        501,
        "Slash-command dispatch is not yet available: the agents service "
        "must publish 'sendSlash' and 'slashCatalog' RPCs first.",
    )


def _require_local_scope_participant(room_id: str, scope: str) -> None:
    room = rooms_svc.get_room(room_id)
    if room is None:
        raise HTTPException(404, f"no such room: {room_id}")
    base_scope, _ = rooms_svc._split_scope(scope)
    for p in rooms_svc.list_participants(room_id, active_only=True):
        if p.kind == "scope" and p.identifier == base_scope:
            return
    raise HTTPException(
        404, f"scope {scope!r} is not an active participant of room {room_id!r}"
    )


# ---------------------------------------------------------------------------
# WebSocket attach
# ---------------------------------------------------------------------------

@router.websocket("/{room_id}/attach")
async def attach_ws(websocket: WebSocket, room_id: str):
    await websocket.accept()
    await rooms_svc.run_subscriber_session(websocket, room_id, "user:operator")
