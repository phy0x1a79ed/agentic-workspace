"""REST + WebSocket router for the rooms surface.

Mounted from ``awm/exposed.py``. Bearer auth applies via the
``require_bearer`` dependency on REST routes; the WS attach endpoint
uses :func:`awm.ws_auth.authenticate_room_ws`.

Cross-peer plumbing (M3) is integrated via ``RemoteRoomProxy`` and the
forward_room_* helpers in ``awm.services.network.federation``.
"""

from __future__ import annotations

from typing import Optional

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Query,
    Request,
    WebSocket,
)

from awm.services.auth.middleware_auth import require_bearer
from awm.models import (
    AgentSlashCatalog,
    AgentSlashRequest,
    AgentSlashResponse,
    LiveAgentState,
    ParticipantInfo,
    PostInfo,
    RoomActionResponse,
    RoomAgentInfo,
    RoomAgentsResponse,
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
    SlashCommandInfo,
)
from awm.services import agent_slash, orchestration, rooms as rooms_svc
from awm.services.auth.ws_auth import authenticate_room_ws


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
    """Resolve the post author from the operator's ``X-Awm-As`` claim.

    User-facing routes intentionally ignore ``X-Awm-From``; that header is
    only consumed by peer-facing routes under ``/peer/...``. Defaults to
    ``user:operator``.
    """
    return request.headers.get("x-awm-as") or "user:operator"


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


@router.get("/search", dependencies=[Depends(require_bearer)])
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


# Cross-peer (M3) receivers live on ``awm/api/peer.py`` under the /peer/
# router; this file no longer hosts dual-mode handlers.


@router.post("/{room_id}/close", response_model=RoomActionResponse,
             dependencies=[Depends(require_bearer)])
def close_room(room_id: str, req: RoomCloseRequest):
    try:
        room = rooms_svc.close_room(room_id, kill_agents=req.kill_agents)
    except rooms_svc.RoomNotFound:
        raise HTTPException(404, f"no such room: {room_id}")
    return RoomActionResponse(message="closed", room=_room_info(room))


@router.post("/{room_id}/archive", response_model=RoomActionResponse,
             dependencies=[Depends(require_bearer)],
             responses={409: {"model": RoomArchiveBlockedResponse}})
def archive_room(room_id: str):
    """Soft-archive a room. 409 with ``blocking_scopes`` if any agent
    (kind='scope') participants are still active."""
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


@router.get("/{room_id}/agents", response_model=RoomAgentsResponse,
            dependencies=[Depends(require_bearer)])
def get_room_agents(room_id: str):
    """Per-agent panel data — list scope/shadow_peer participants and,
    for local scopes, attach their agent-instance state from
    ``agent_instances._by_scope``."""
    room = rooms_svc.get_room(room_id)
    if room is None:
        raise HTTPException(404, f"no such room: {room_id}")
    from awm.services import agent_instances

    agents: list[RoomAgentInfo] = []
    for p in rooms_svc.list_participants(room_id, active_only=False):
        if p.kind not in ("scope", "shadow_peer"):
            continue
        live: LiveAgentState | None = None
        if p.kind == "scope":
            base_scope, peer = rooms_svc._split_scope(p.identifier)
            if peer is None:
                session = agent_instances._by_scope.get(base_scope)
                if session is not None:
                    live = LiveAgentState(
                        pid=getattr(session.proc, "pid", None),
                        status=session.status,
                        started_at=session.started_at,
                        exited_at=session.exited_at,
                        exit_code=session.exit_code,
                        agent_cli=session.agent_cli,
                        permission_mode=session.permission_mode,
                        model=session.model,
                        effort=session.effort,
                        claude_session_id=session.claude_session_id,
                        context_used=session.context_used,
                        context_max=session.context_max,
                    )
        agents.append(RoomAgentInfo(
            scope=p.identifier, kind=p.kind, identifier=p.identifier,
            joined_at=p.joined_at, live=live,
        ))
    return RoomAgentsResponse(agents=agents)


# ---------------------------------------------------------------------------
# Agent slash-command surface
# ---------------------------------------------------------------------------

def _require_local_scope_participant(room_id: str, scope: str) -> None:
    """Reject if scope isn't a local scope participant in the room.

    Slash commands are agent-control and must be ``to:``-targeted; broadcast
    or untargeted slashes are refused at the API edge.
    """
    room = rooms_svc.get_room(room_id)
    if room is None:
        raise HTTPException(404, f"no such room: {room_id}")
    base_scope, peer = rooms_svc._split_scope(scope)
    if peer is not None:
        raise HTTPException(
            400, f"slash commands on remote scopes are not yet supported: {scope!r}"
        )
    for p in rooms_svc.list_participants(room_id, active_only=True):
        if p.kind == "scope" and p.identifier == scope:
            return
    raise HTTPException(
        404, f"scope {scope!r} is not an active participant of room {room_id!r}"
    )


@router.get(
    "/{room_id}/agents/{scope:path}/slash-commands",
    response_model=AgentSlashCatalog,
    dependencies=[Depends(require_bearer)],
)
def get_agent_slash_commands(room_id: str, scope: str) -> AgentSlashCatalog:
    """Catalog of slash commands for the given scope.

    Returns server-controlled commands plus any commands the scope's live
    claude reported in its init event (claude-specific list).
    """
    _require_local_scope_participant(room_id, scope)
    from awm.services import agent_instances
    sess = agent_instances._by_scope.get(scope)
    claude_cmds = list(sess.claude_slash_commands) if sess is not None else []
    return AgentSlashCatalog(
        server=[SlashCommandInfo(**c) for c in agent_slash.server_catalog()],
        claude=claude_cmds,
    )


@router.post(
    "/{room_id}/agents/{scope:path}/slash",
    response_model=AgentSlashResponse,
    dependencies=[Depends(require_bearer)],
)
async def post_agent_slash(
    room_id: str, scope: str, req: AgentSlashRequest, request: Request,
) -> AgentSlashResponse:
    """Run a slash command against a scope's agent.

    If the leading token matches a server command, the registered handler
    runs and its message is posted to the room as ``system``. Otherwise
    the line is forwarded unframed to the scope's claude stdin so /clear,
    /compact, plugin commands etc. still work.

    Either way the original command is recorded in the room transcript as
    kind=``slash`` (authored by the caller) for audit.
    """
    _require_local_scope_participant(room_id, scope)
    line = (req.cmd or "").strip()
    if not line.startswith("/"):
        raise HTTPException(400, "slash commands must start with '/'")

    author = _opener_from_request(request)
    # Audit-log the invocation in the room first.
    try:
        rooms_svc.post(room_id, author=author, body=line, kind="slash",
                       to_scope=scope)
    except rooms_svc.RoomError as exc:
        raise HTTPException(409, str(exc))

    try:
        handled, message = await agent_slash.dispatch(scope, line)
    except agent_slash.SlashParseError as exc:
        raise HTTPException(400, str(exc))
    except ValueError as exc:
        raise HTTPException(400, str(exc))

    if handled:
        # Post the server response as a system message so all subscribers
        # see what happened.
        try:
            rooms_svc.post(room_id, author="system", body=message, kind="system")
        except rooms_svc.RoomError:
            pass
        return AgentSlashResponse(handled=True, result=message)

    # Unknown server command — forward raw to the scope's claude stdin.
    from awm.services import agent_instances
    try:
        await agent_instances.send_slash(scope, line)
    except agent_instances.NoSessionError as exc:
        raise HTTPException(409, str(exc))
    return AgentSlashResponse(handled=False, result="")


# ---------------------------------------------------------------------------
# WebSocket attach — thin: auth + accept + delegate to services.rooms
# ---------------------------------------------------------------------------

@router.websocket("/{room_id}/attach")
async def attach_ws(websocket: WebSocket, room_id: str):
    auth = await authenticate_room_ws(websocket)
    if not auth.ok:
        return
    await websocket.accept(subprotocol=auth.subprotocol)
    await rooms_svc.run_subscriber_session(websocket, room_id, auth.user_as)
