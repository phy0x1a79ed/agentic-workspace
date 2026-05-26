"""Vagrant-session endpoint: provision a per-user vagrant scope + default room.

Mounted from :mod:`awm.exposed` at ``POST /vagrant/session``. The web-UI calls
this after sign-in so the SPA can land the user inside a room owned by their
vagrant scope on the host peer. We also (idempotently) spawn a Claude
``AgentInstance`` against the vagrant scope so the user has a live "manager"
ready to receive room posts addressed to it.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from awm.config import VAGRANT_PROJECT
from awm.middleware_auth import require_bearer
from awm.services import agent_instances
from awm.services.scopes import (
    _vagrant_scope_name,
    ensure_vagrant_session,
    vagrant_scope_identifier,
)


log = logging.getLogger("awm.api.vagrant")


router = APIRouter(prefix="/vagrant", tags=["vagrant"])


class VagrantSessionResponse(BaseModel):
    scope_uuid: str
    room_id: str
    # ``project/scope`` identifier for the vagrant scope. Matches the
    # ``scope`` field of ``GET /rooms/{id}/agents`` so the UI can mark
    # which agent in a room is "the manager".
    scope_identifier: str
    # True once a Claude AgentInstance is registered for the vagrant
    # scope (the manager can receive posts). False if auto-spawn failed —
    # the UI surfaces a hint so the user can restart manually.
    manager_live: bool


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

    scope_name = _vagrant_scope_name(user_as)
    manager_live = True
    if agent_instances.get_session_by_scope(VAGRANT_PROJECT, scope_name) is None:
        try:
            await agent_instances.create_session(
                project=VAGRANT_PROJECT,
                scope=scope_name,
                agent_cli="claude",
            )
        except agent_instances.ScopeBusyError:
            # Race with another spawn — that's success for our purposes.
            pass
        except Exception:  # noqa: BLE001
            log.exception("vagrant manager spawn failed for %s", user_as)
            manager_live = False

    return VagrantSessionResponse(
        scope_uuid=scope_uuid,
        room_id=room_id,
        scope_identifier=vagrant_scope_identifier(user_as),
        manager_live=manager_live,
    )
