"""Vagrant-session endpoint: provision a per-user vagrant scope + default room.

Mounted from :mod:`awm.exposed` at ``POST /vagrant/session``. The web-UI calls
this after sign-in so the SPA can land the user inside a room owned by their
vagrant scope on the host peer.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from awm.middleware_auth import require_bearer
from awm.services.scopes import ensure_vagrant_session, vagrant_scope_identifier


router = APIRouter(prefix="/vagrant", tags=["vagrant"])


class VagrantSessionResponse(BaseModel):
    scope_uuid: str
    room_id: str
    # ``project/scope`` identifier for the vagrant scope. Matches the
    # ``scope`` field of ``GET /rooms/{id}/agents`` so the UI can mark
    # which agent in a room is "the manager".
    scope_identifier: str


def _user_from_request(request: Request) -> str:
    return request.headers.get("x-awm-as") or "user:operator"


@router.post(
    "/session",
    response_model=VagrantSessionResponse,
    dependencies=[Depends(require_bearer)],
)
async def session(request: Request) -> VagrantSessionResponse:
    user_as = _user_from_request(request)
    try:
        scope_uuid, room_id = ensure_vagrant_session(user_as)
    except FileNotFoundError as exc:
        # 503: server-side bootstrap missing (run `awm vagrant-init`).
        raise HTTPException(status_code=503, detail=str(exc))
    return VagrantSessionResponse(
        scope_uuid=scope_uuid,
        room_id=room_id,
        scope_identifier=vagrant_scope_identifier(user_as),
    )
