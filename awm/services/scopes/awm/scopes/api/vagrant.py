"""Vagrant-session endpoint: provision a per-user vagrant scope + default room.

Mounted at ``POST /vagrant/session``. The web-UI calls this after sign-in
so the SPA can land the user inside a room owned by their vagrant scope.

v1 modular: agent_instances is the agents service (out-of-process).
Manager spawn is deferred via gatewayclient.call('agents', 'createSession', ...)
once the agents service publishes that RPC. Until then, ``manager_live``
always returns False (the scope and room are still created; the manager
agent is just not auto-spawned).

BLOCKER: agents RPC ``createSession(project, scope, ...)`` not yet in
the frozen contract. When it is, replace the stub below with:
    await gatewayclient.call('agents', 'createSession', {
        'project': VAGRANT_PROJECT,
        'scope': scope_name,
        'agent_cli': 'claude',
        'permission_mode': 'bypassPermissions',
    })
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from awm.config import VAGRANT_PROJECT
from awm.scopes.scopes import (
    _vagrant_scope_name,
    ensure_vagrant_session,
    vagrant_scope_identifier,
)


log = logging.getLogger("awm.scopes.api.vagrant")


router = APIRouter(prefix="/vagrant", tags=["vagrant"])


class VagrantSessionResponse(BaseModel):
    scope_uuid: str
    room_id: str
    scope_identifier: str
    manager_live: bool


@router.post("/session", response_model=VagrantSessionResponse)
async def session(request: Request) -> VagrantSessionResponse:
    user_as = "user:operator"
    try:
        scope_uuid, room_id = ensure_vagrant_session(user_as)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=503, detail=str(exc))

    scope_name = _vagrant_scope_name(user_as)
    manager_live = False
    # STUB: agents service RPC createSession not yet published.
    # When agents exposes it, wire:
    #   try:
    #       import awm.gatewayclient as gatewayclient
    #       await gatewayclient.call('agents', 'createSession', {
    #           'project': VAGRANT_PROJECT, 'scope': scope_name,
    #           'agent_cli': 'claude', 'permission_mode': 'bypassPermissions',
    #       })
    #       manager_live = True
    #   except Exception:
    #       log.exception("vagrant manager spawn failed for %s", user_as)
    log.info(
        "vagrant session created for %s: scope=%s/%s room=%s "
        "(manager_live=False — agents RPC createSession not yet wired)",
        user_as, VAGRANT_PROJECT, scope_name, room_id,
    )

    return VagrantSessionResponse(
        scope_uuid=scope_uuid,
        room_id=room_id,
        scope_identifier=vagrant_scope_identifier(user_as),
        manager_live=manager_live,
    )
