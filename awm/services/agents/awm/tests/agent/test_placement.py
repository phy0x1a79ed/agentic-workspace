"""place_on_task — provisioning order, minted ids, placement row, kickoff, respawn."""

from __future__ import annotations

import asyncio

import pytest

pytestmark = [pytest.mark.agent, pytest.mark.smoke]

import awm.agents.agent_instances as ai_mod
from awm.agents import placement
from awm.agents.dao import AgentsDAO


def _names(calls):
    return [(svc, fn) for (svc, fn, _a) in calls]


async def _place(agents_env, **over):
    args = {"task_id": "T-1", "project": "p", "scope_slug": "leaf-1",
            "brief": "do the thing", "contracts_in": ["in:1"],
            "contracts_out": ["out:1"], "mode": "worker"}
    args.update(over)
    return await placement.place_on_task(args)


class TestPlaceOnTask:
    async def test_scope_create_before_spawn(self, agents_env, stub_core):
        await _place(agents_env)
        # scope_create must be recorded before the spawn (open_agent) ran.
        assert ("scopes", "scope_create") in _names(agents_env["calls"])
        sc_idx = _names(agents_env["calls"]).index(("scopes", "scope_create"))
        # ensureProject/ensureScope happen INSIDE create_session (after spawn
        # provisioning) → they come after scope_create.
        assert sc_idx == 0
        assert len(stub_core["opened"]) == 1  # spawned exactly once

    async def test_minted_ids_shape(self, agents_env, stub_core):
        res = await _place(agents_env)
        assert res["agent_ref"].startswith("agt-")
        assert res["placement_token"].startswith("plt-")
        assert res["scope"] == "leaf-1"

    async def test_persists_placement_row(self, agents_env, stub_core):
        res = await _place(agents_env)
        dao = AgentsDAO()
        row = dao.resolve_placement(res["placement_token"])
        assert row is not None
        assert row["mode"] == "worker"
        assert row["task_ref"] == "T-1"
        assert row["agent_ref"] == res["agent_ref"]

    async def test_kickoff_enqueued(self, agents_env, stub_core):
        await _place(agents_env)
        # The kickoff is enqueued onto stdin and pumped to the fake session.
        await asyncio.sleep(0.05)
        sent = stub_core["session"].sent
        assert any("Begin now" in s for s in sent)

    async def test_brief_written_as_context(self, agents_env, stub_core):
        res = await _place(agents_env)
        ctx_arg = next(a for (svc, fn, a) in agents_env["calls"]
                       if fn == "scope_create")["context"]
        assert "T-1" in ctx_arg
        assert res["placement_token"] in ctx_arg
        assert "task_deliver" in ctx_arg  # how-to-finish instructions present

    async def test_agent_ref_stable_across_respawn(self, agents_env, stub_core):
        res = await _place(agents_env)
        token = res["placement_token"]
        first = ai_mod.get_session_by_scope("p", "leaf-1")
        assert first.agent_ref == res["agent_ref"]

        new = await ai_mod.respawn_session("p/leaf-1")

        assert new.id != first.id
        assert new.agent_ref == res["agent_ref"]      # identity carried forward
        assert new.mode == "worker"
        assert new.placement_token == token            # token stable
        # The OLD row released the token (partial-unique invariant) and the new
        # row holds it; resolution finds the new row.
        dao = AgentsDAO()
        assert dao.resolve_placement(token)["id"] == new.id
        old = dao.get_instance(first.id)
        assert old["placement_token"] is None
