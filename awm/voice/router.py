"""FastAPI router for the voice side panel.

Endpoints:
  WS   /voice/ws               per-user voice WebSocket (PTT, transcripts, audio out)
  GET  /voice/state            agent status for the calling user
  POST /voice/rooms/{room_id}/join   voice agent joins a room as participant
  POST /voice/rooms/{room_id}/leave  voice agent leaves a room
  POST /voice/text             typed input (debug / accessibility)

Auth: HTTP routes use ``Depends(require_bearer)``; WS uses the
``bearer.<token>`` subprotocol via ``authenticate_room_ws``. User
identity is derived from the ``X-Awm-As`` header (defaults to
``user:operator``).
"""

from __future__ import annotations

import logging

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Request,
    WebSocket,
)
from pydantic import BaseModel

from awm.middleware_auth import require_bearer
from awm.services import rooms as rooms_svc
from awm.voice.registry import get_registry
from awm.ws_auth import authenticate_room_ws


log = logging.getLogger("awm.voice.router")


router = APIRouter(prefix="/voice", tags=["voice"])


def _user_from_request(request: Request) -> str:
    user_as = request.headers.get("x-awm-as") or "user:operator"
    from_peer = request.headers.get("x-awm-from")
    if from_peer and "@" not in user_as:
        user_as = f"{user_as}@{from_peer}"
    return user_as


class VoiceStateResponse(BaseModel):
    user: str
    joined_rooms: list[str]
    connected_clients: int
    recording: bool
    has_turn: bool


@router.get(
    "/state",
    response_model=VoiceStateResponse,
    dependencies=[Depends(require_bearer)],
)
async def get_state(request: Request) -> VoiceStateResponse:
    user = _user_from_request(request)
    reg = get_registry()
    agent = await reg.get_or_create(user)
    return VoiceStateResponse(
        user=user,
        joined_rooms=sorted(agent.joined_rooms),
        connected_clients=len(agent.clients),
        recording=agent.recording_client is not None,
        has_turn=agent.turn is not None,
    )


class TextInputRequest(BaseModel):
    body: str


@router.post("/text", dependencies=[Depends(require_bearer)])
async def post_text(req: TextInputRequest, request: Request) -> dict:
    user = _user_from_request(request)
    reg = get_registry()
    agent = await reg.get_or_create(user)
    await agent.handle_text(req.body)
    return {"ok": True}


class RoomJoinResponse(BaseModel):
    room_id: str
    user: str
    joined: bool


@router.post(
    "/rooms/{room_id}/join",
    response_model=RoomJoinResponse,
    dependencies=[Depends(require_bearer)],
)
async def join_room(room_id: str, request: Request) -> RoomJoinResponse:
    user = _user_from_request(request)
    reg = get_registry()
    agent = await reg.get_or_create(user)
    try:
        rooms_svc.add_participant(room_id, "voice", user)
    except rooms_svc.RoomNotFound:
        raise HTTPException(404, f"no such room: {room_id}")
    except rooms_svc.RoomClosed:
        raise HTTPException(409, f"room {room_id} is closed")
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    agent.joined_rooms.add(room_id)
    await agent.broadcast_json({
        "type": "joined_rooms",
        "rooms": sorted(agent.joined_rooms),
    })
    return RoomJoinResponse(room_id=room_id, user=user, joined=True)


@router.post(
    "/rooms/{room_id}/leave",
    response_model=RoomJoinResponse,
    dependencies=[Depends(require_bearer)],
)
async def leave_room(room_id: str, request: Request) -> RoomJoinResponse:
    user = _user_from_request(request)
    reg = get_registry()
    agent = await reg.get_or_create(user)
    rooms_svc.remove_participant(room_id, "voice", user)
    agent.joined_rooms.discard(room_id)
    await agent.broadcast_json({
        "type": "joined_rooms",
        "rooms": sorted(agent.joined_rooms),
    })
    return RoomJoinResponse(room_id=room_id, user=user, joined=False)


@router.websocket("/ws")
async def voice_ws(websocket: WebSocket) -> None:
    auth = await authenticate_room_ws(websocket)
    if not auth.ok:
        return
    await websocket.accept(subprotocol=auth.subprotocol)
    # Optional ?room_id=... binds the voice turn output to a room, so agent
    # replies land in the room transcript alongside any human posts.
    room_id = websocket.query_params.get("room_id") or None
    from awm.voice.registry import run_voice_ws_session
    await run_voice_ws_session(websocket, auth.user_as, room_id=room_id)
