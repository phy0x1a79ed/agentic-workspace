"""place_on_task — workspace provisioning, minted ids, placement row, kickoff,
per-mode tool profiles, respawn carry-forward."""

from __future__ import annotations

import asyncio
import json

import pytest

pytestmark = [pytest.mark.agent, pytest.mark.smoke]

import awm.agents.agent_instances as ai_mod
from awm.agents import placement
from awm.agents.dao import AgentsDAO


def _names(calls):
    return [(svc, fn) for (svc, fn, _a) in calls]


async def _place(agents_env, **over):
    args = {"task_id": "T-1", "unit_slug": "leaf-1",
            "brief": "do the thing", "contracts_in": ["in:1"],
            "contracts_out": ["out:1"], "mode": "worker"}
    args.update(over)
    return await placement.place_on_task(args)


class TestPlaceOnTask:
    async def test_workspace_create_before_spawn(self, agents_env, stub_core):
        await _place(agents_env)
        names = _names(agents_env["calls"])
        # The unit is provisioned via the workspace service — NOT scopes.
        assert ("workspace", "workspace_create") in names
        assert not any(svc == "scopes" for (svc, _fn) in names)
        assert names.index(("workspace", "workspace_create")) == 0
        assert len(stub_core["opened"]) == 1  # spawned exactly once

    async def test_minted_ids_shape(self, agents_env, stub_core):
        res = await _place(agents_env)
        assert res["agent_ref"].startswith("agt-")
        assert res["placement_token"].startswith("plt-")
        assert res["unit_slug"] == "leaf-1"

    async def test_persists_placement_row_with_spec(self, agents_env, stub_core):
        res = await _place(agents_env)
        dao = AgentsDAO()
        row = dao.resolve_placement(res["placement_token"])
        assert row is not None
        assert row["mode"] == "worker"
        assert row["task_ref"] == "T-1"
        data = json.loads(row["data"])
        spec = data["placement"]
        assert spec["mode"] == "worker"
        assert spec["contracts_out"] == ["out:1"]
        assert spec["workspace_path"].endswith("/leaf-1")
        assert data["done"] is False and data["staged"] == {}

    async def test_spawn_carries_worker_tool_profile(self, agents_env, stub_core):
        await _place(agents_env)
        cfg = stub_core["opened"][0]
        # full fs built-ins + the worker MCP tools, scoped by the allowlist.
        assert "Write" in cfg.allowed_tools
        assert "mcp__awm__edit_deliverable" in cfg.allowed_tools
        assert "mcp__awm__add_subtask" not in cfg.allowed_tools

    async def test_kickoff_enqueued(self, agents_env, stub_core):
        await _place(agents_env)
        await asyncio.sleep(0.05)
        sent = stub_core["session"].sent
        assert any("Begin now" in s for s in sent)

    async def test_brief_written_as_context_md(self, agents_env, stub_core):
        res = await _place(agents_env)
        ctx = next(a for (svc, fn, a) in agents_env["calls"]
                   if fn == "workspace_create")["context_md"]
        assert "T-1" in ctx
        # The placement token never reaches the model's surface anymore — its
        # tools resolve from its own identity (T1: kill the token footgun).
        assert res["placement_token"] not in ctx
        assert "edit_deliverable" in ctx  # how-to-finish present
        assert "task_deliver" not in ctx  # old surface gone

    async def test_agent_ref_and_workdir_stable_across_respawn(
            self, agents_env, stub_core):
        res = await _place(agents_env)
        token = res["placement_token"]
        first = ai_mod.get_session_by_scope("leaf-1")
        assert first.agent_ref == res["agent_ref"]
        assert first.workdir.endswith("/leaf-1")

        new = await ai_mod.respawn_session("leaf-1")

        assert new.id != first.id
        assert new.agent_ref == res["agent_ref"]      # identity carried forward
        assert new.mode == "worker"
        assert new.placement_token == token            # token stable
        assert new.workdir == first.workdir            # unit carried forward
        assert "mcp__awm__edit_deliverable" in new.allowed_tools
        dao = AgentsDAO()
        assert dao.resolve_placement(token)["id"] == new.id
        assert dao.get_instance(first.id)["placement_token"] is None


class TestModeProfiles:
    async def test_plan_mode_is_read_only_and_defaults_plan_contract(
            self, agents_env, stub_core):
        res = await _place(agents_env, mode="plan", contracts_out=[])
        cfg = stub_core["opened"][0]
        assert "Write" not in cfg.allowed_tools          # read-only fs
        assert "Read" in cfg.allowed_tools
        assert "mcp__awm__edit_deliverable" in cfg.allowed_tools
        row = AgentsDAO().resolve_placement(res["placement_token"])
        assert json.loads(row["data"])["placement"]["contracts_out"] == ["plan"]

    async def test_verify_mode_has_no_filesystem_tools(
            self, agents_env, stub_core):
        await _place(agents_env, mode="verify", unit_slug="leaf-v")
        cfg = stub_core["opened"][0]
        for fs in ("Read", "Write", "Edit", "Bash", "Grep", "Glob"):
            assert fs not in (cfg.allowed_tools or [])
        assert "mcp__awm__approve_plan" in cfg.allowed_tools
        assert "mcp__awm__reject_plan" in cfg.allowed_tools

    async def test_verify_kickoff_carries_objective_and_plan(
            self, agents_env, stub_core):
        # Pre-stage a plan deliverable into the (idempotent) unit so the verifier
        # kickoff can embed it (the verifier has no fs to read it itself).
        from pathlib import Path
        unit = (agents_env["awm_dir"] / "services" / "workspace" / "units"
                / "leaf-v")
        plan_dir = unit / "deliverable" / "plan"
        plan_dir.mkdir(parents=True, exist_ok=True)
        (plan_dir / "payload").write_text("THE PLAN BODY")

        await _place(agents_env, mode="verify", unit_slug="leaf-v",
                     brief="ship X")
        await asyncio.sleep(0.05)
        sent = "\n".join(stub_core["session"].sent)
        assert "PLAN VERIFIER" in sent
        assert "ship X" in sent
        assert "THE PLAN BODY" in sent

    async def test_planner_mode_gets_graph_tools(self, agents_env, stub_core):
        await _place(agents_env, mode="planner", unit_slug="leaf-pl")
        cfg = stub_core["opened"][0]
        assert "mcp__awm__add_subtask" in cfg.allowed_tools
        assert "Write" not in cfg.allowed_tools

    async def test_unknown_mode_rejected(self, agents_env, stub_core):
        with pytest.raises(ValueError):
            await _place(agents_env, mode="bogus")
