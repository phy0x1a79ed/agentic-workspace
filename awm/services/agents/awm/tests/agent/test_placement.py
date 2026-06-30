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
        res = await _place(agents_env)
        cfg = stub_core["opened"][0]
        # Full fs built-ins + the single collapsed agent-domain tool. Per-verb
        # scoping moved server-side (VERB_PROFILES) — allowed_tools no longer
        # names individual placement verbs.
        assert "Write" in cfg.allowed_tools
        assert "mcp__awm__agent" in cfg.allowed_tools
        assert not any(t.startswith("mcp__awm__") and t != "mcp__awm__agent"
                       for t in cfg.allowed_tools)
        # The per-mode verb allowlist is recorded on the row for the server gate.
        data = json.loads(AgentsDAO().resolve_placement(res["placement_token"])["data"])
        assert "edit_deliverable" in data["allowed_verbs"]
        assert "add_subtask" not in data["allowed_verbs"]  # planner-only verb

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
        assert "mcp__awm__agent" in new.allowed_tools
        dao = AgentsDAO()
        assert dao.resolve_placement(token)["id"] == new.id
        assert dao.get_instance(first.id)["placement_token"] is None


class TestWaitForUserAndPaused:
    async def test_empty_goal_worker_idles_no_kickoff(self, agents_env, stub_core):
        # The "+ new task" button path: an attended worker on an EMPTY goal is
        # placed but NOT kicked off — it idles for the human's first turn.
        await _place(agents_env, brief=json.dumps({"goal": "", "mode": "worker"}),
                     attached=True, paused=True)
        await asyncio.sleep(0.05)
        assert stub_core["session"].sent == []          # no kickoff injected

    async def test_nonempty_goal_worker_still_kicks_off(self, agents_env, stub_core):
        await _place(agents_env,
                     brief=json.dumps({"goal": "do it", "mode": "worker"}))
        await asyncio.sleep(0.05)
        assert any("Begin now" in s for s in stub_core["session"].sent)

    async def test_paused_seeded_onto_instance_data(self, agents_env, stub_core):
        res = await _place(agents_env, paused=True)
        data = json.loads(
            AgentsDAO().resolve_placement(res["placement_token"])["data"])
        assert data["paused"] is True


class TestServerSideVerbGate:
    """ensure_verb_allowed — the server-side per-verb confinement that replaces
    claude --allowedTools for the collapsed `agent` domain (and is the ONLY
    enforcement under the default opencode harness)."""

    async def test_allows_in_profile_verb(self, agents_env, stub_core):
        await _place(agents_env, mode="worker")
        # worker may stage deliverables — no raise.
        placement.ensure_verb_allowed("leaf-1", "edit_deliverable")

    async def test_rejects_out_of_profile_verb(self, agents_env, stub_core):
        await _place(agents_env, mode="worker")
        # add_subtask is planner-only → rejected for a worker.
        with pytest.raises(placement.PlacementError):
            placement.ensure_verb_allowed("leaf-1", "add_subtask")

    async def test_verify_cannot_deliver(self, agents_env, stub_core):
        await _place(agents_env, mode="verify", unit_slug="leaf-v")
        with pytest.raises(placement.PlacementError):
            placement.ensure_verb_allowed("leaf-v", "edit_deliverable")
        # but its own verdict verbs pass
        placement.ensure_verb_allowed("leaf-v", "approve_plan")

    async def test_task_fail_always_allowed(self, agents_env, stub_core):
        await _place(agents_env, mode="verify", unit_slug="leaf-v")
        placement.ensure_verb_allowed("leaf-v", "task_fail")

    async def test_unknown_identity_rejected(self, agents_env, stub_core):
        with pytest.raises(placement.PlacementError):
            placement.ensure_verb_allowed("no-such-placement", "edit_deliverable")


class TestModeProfiles:
    async def test_plan_mode_is_read_only_and_defaults_plan_contract(
            self, agents_env, stub_core):
        res = await _place(agents_env, mode="plan", contracts_out=[])
        cfg = stub_core["opened"][0]
        assert "Write" not in cfg.allowed_tools          # read-only fs
        assert "Read" in cfg.allowed_tools
        assert "mcp__awm__agent" in cfg.allowed_tools
        row = AgentsDAO().resolve_placement(res["placement_token"])
        data = json.loads(row["data"])
        assert data["placement"]["contracts_out"] == ["plan"]
        assert "edit_deliverable" in data["allowed_verbs"]

    async def test_verify_mode_has_no_filesystem_tools(
            self, agents_env, stub_core):
        res = await _place(agents_env, mode="verify", unit_slug="leaf-v")
        cfg = stub_core["opened"][0]
        for fs in ("Read", "Write", "Edit", "Bash", "Grep", "Glob"):
            assert fs not in (cfg.allowed_tools or [])
        # Only the collapsed agent domain; the verify verbs are gated server-side.
        assert cfg.allowed_tools == ["mcp__awm__agent"]
        data = json.loads(AgentsDAO().resolve_placement(res["placement_token"])["data"])
        assert set(data["allowed_verbs"]) == {"approve_plan", "reject_plan", "task_fail"}

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
        res = await _place(agents_env, mode="planner", unit_slug="leaf-pl")
        cfg = stub_core["opened"][0]
        assert "mcp__awm__agent" in cfg.allowed_tools
        assert "Write" not in cfg.allowed_tools          # read-only fs
        data = json.loads(AgentsDAO().resolve_placement(res["placement_token"])["data"])
        assert "add_subtask" in data["allowed_verbs"]    # graph verbs permitted

    async def test_unknown_mode_rejected(self, agents_env, stub_core):
        with pytest.raises(ValueError):
            await _place(agents_env, mode="bogus")
