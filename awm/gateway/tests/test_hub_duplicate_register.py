"""Duplicate-instance rejection at /hub/service/register (lifecycle plan T3).

When a service name already has a record whose control-WS lease is *held* (a
live instance is connected), a second instance registering under that name is
turned away with 409 and the incumbent is left untouched. A record whose lease
is NOT held is dead, so takeover (replace-in-place) still works.

The guard lives in the endpoint, not the registry, so these drive the
``service_register`` endpoint function directly.
"""

from __future__ import annotations

import asyncio

import pytest
pytestmark = [pytest.mark.hub]

from fastapi import HTTPException

from awm.gateway.api.hub import ServiceRegisterRequest, service_register
from awm.gateway.hub import lease as lease_mod
from awm.gateway.hub import registry as reg_mod
from awm.gateway.hub import rpc as rpc_mod


@pytest.fixture(autouse=True)
def _isolate(awm_workspace):
    reg_mod._singleton = reg_mod.Registry()
    lease_mod._singleton = None  # rebound to the fresh registry on next get
    rpc_mod._channels.clear()
    yield
    reg_mod._singleton = reg_mod.Registry()
    lease_mod._singleton = None
    rpc_mod._channels.clear()


def _hold_lease(service_id: str) -> None:
    """Mark ``service_id``'s lease as held (a live instance is connected)."""
    lm = lease_mod.get_lease_manager()
    lm._holders[service_id] = asyncio.Event()


async def _register_base(name: str = "scopes"):
    return await reg_mod.get_registry().register_service(
        name, f"/svc/{name}", pid=111, start_cmd=["bash", "run.sh"], cwd="/x")


def test_live_lease_rejects_duplicate_and_keeps_incumbent():
    async def go():
        rec = await _register_base("scopes")
        _hold_lease(rec.service_id)
        with pytest.raises(HTTPException) as ei:
            await service_register(ServiceRegisterRequest(name="scopes"))
        assert ei.value.status_code == 409
        # Incumbent's record + service_id untouched; lease still held.
        survivor = reg_mod.get_registry().get_by_name("service", "scopes")
        assert survivor is not None
        assert survivor.service_id == rec.service_id
        assert lease_mod.get_lease_manager().is_held(rec.service_id)

    asyncio.run(go())


def test_dead_record_allows_takeover():
    async def go():
        rec = await _register_base("scopes")
        # Lease NOT held → the incumbent is dead; takeover replaces in place.
        resp = await service_register(ServiceRegisterRequest(name="scopes"))
        assert resp.name == "scopes"
        assert resp.service_id != rec.service_id
        survivor = reg_mod.get_registry().get_by_name("service", "scopes")
        assert survivor.service_id == resp.service_id

    asyncio.run(go())


def test_overlay_branch_unaffected_by_guard():
    """An overlay register hits the req.overlay branch, never the guard — even
    when the base's lease is held (a shadow stacks on a live base)."""
    async def go():
        rec = await _register_base("scopes")
        _hold_lease(rec.service_id)
        resp = await service_register(ServiceRegisterRequest(
            name="scopes-shadow", prefix="/svc/scopes", overlay=True))
        assert resp.name == "scopes-shadow"
        # Base incumbent still present underneath the overlay.
        assert reg_mod.get_registry().get_by_name(
            "service", "scopes").service_id == rec.service_id

    asyncio.run(go())
