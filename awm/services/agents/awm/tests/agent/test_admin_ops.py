"""The attach-gated admin registry (T3): one uniform gate over a configurable
op list, with the worker tool profile + the manifest both derived from it.

An admin command (relocate_self / create_node / depend_on / link_repo) is usable
ONLY while a human is attached: ``placement.relay_admin`` resolves the caller's
placement, re-checks the attached flag every call, and forwards to the named
orchestrator op as the resolved {task_id, agent_ref}. Adding/removing a command
is a single edit in ``admin_ops`` — proven here by deriving the manifest + tool
profile + handlers from the registry."""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.agent, pytest.mark.smoke]

from awm.agents import admin_ops
from awm.agents import placement
from awm.agents import agent_instances as ai_mod


async def _place_worker(agents_env, **over):
    args = {"task_id": "T-1", "unit_slug": "leaf-1",
            "brief": "do it", "contracts_out": ["out:1"], "mode": "worker"}
    args.update(over)
    return await placement.place_on_task(args)


def _orch_calls(agents_env, op):
    return [a for (svc, fn, a) in agents_env["calls"]
            if svc == "orchestrator" and fn == op]


class TestRegistryDerivation:
    def test_worker_profile_permits_every_admin_verb(self):
        # The admin verbs are now permitted server-side (VERB_PROFILES), not via
        # claude --allowedTools (which carries only the collapsed agent domain).
        worker_verbs = placement.VERB_PROFILES["worker"]
        for name in admin_ops.ADMIN_TOOL_NAMES:
            assert name in worker_verbs
        # plan/verify/planner do NOT get the DAG-RESTRUCTURING admin verbs.
        # plan & planner DO carry the ``set_title`` label seam — steering-time
        # label derivation happens on any attended node (attach-on-rest can
        # attend a plan/planner leg) — while verify stays verdict-only.
        restructuring = [n for n in admin_ops.ADMIN_TOOL_NAMES if n != "set_title"]
        for mode in ("plan", "verify", "planner"):
            assert not any(n in placement.VERB_PROFILES[mode]
                           for n in restructuring)
        assert "set_title" in placement.VERB_PROFILES["plan"]
        assert "set_title" in placement.VERB_PROFILES["planner"]
        assert "set_title" not in placement.VERB_PROFILES["verify"]
        # Every mode's allowed_tools is just the one agent-domain tool (+ fs).
        for mode in ("worker", "plan", "planner", "verify"):
            assert "mcp__awm__agent" in placement.TOOL_PROFILES[mode]

    def test_manifest_and_handlers_cover_the_registry(self):
        from awm.agents import hub_adapter
        tools = {f.get("tool") or f["name"]
                 for f in hub_adapter.API_MANIFEST["functions"]}
        for name in admin_ops.ADMIN_TOOL_NAMES:
            # Admin ops fold into the agent domain (tool == f"agent_{name}").
            assert f"agent_{name}" in tools
            assert name in hub_adapter.HANDLERS


class TestAttachGate:
    async def test_detached_admin_op_is_rejected(self, agents_env, stub_core):
        await _place_worker(agents_env)
        # No human attached → the gate rejects, and nothing reaches the kernel.
        with pytest.raises(placement.PlacementError):
            await placement.relay_admin("relocate_self", {}, as_="leaf-1")
        assert _orch_calls(agents_env, "relocate_task") == []

    async def test_attached_admin_op_forwards_resolved_refs(
            self, agents_env, stub_core):
        res = await _place_worker(agents_env)
        await placement.set_attached("leaf-1", True)

        out = await placement.relay_admin(
            "relocate_self", {"new_consumer_task": "T-9"}, as_="leaf-1")
        assert out["ok"] is True and out["op"] == "relocate_self"

        calls = _orch_calls(agents_env, "relocate_task")
        assert len(calls) == 1
        payload = calls[0]
        assert payload["task_id"] == "T-1"            # resolved from identity
        assert payload["agent_ref"] == res["agent_ref"]
        assert payload["new_consumer_task"] == "T-9"  # forwarded arg
        assert "placement_token" not in payload       # never leaks the token

    async def test_create_node_forwards_goal_no_project(
            self, agents_env, stub_core):
        await _place_worker(agents_env)
        await placement.set_attached("leaf-1", True)
        await placement.relay_admin(
            "create_node", {"goal": "a sub-thing"}, as_="leaf-1")
        calls = _orch_calls(agents_env, "orch_task_attach")
        assert len(calls) == 1
        assert calls[0]["goal"] == "a sub-thing"
        # No project is injected anymore — a task has no project namespace.
        assert "project" not in calls[0]

    async def test_link_repo_forwards_repo(self, agents_env, stub_core):
        await _place_worker(agents_env)
        await placement.set_attached("leaf-1", True)
        await placement.relay_admin(
            "link_repo", {"repo": "my-scope"}, as_="leaf-1")
        calls = _orch_calls(agents_env, "link_repo")
        assert calls and calls[0]["repo"] == "my-scope"

    async def test_detach_re_locks_admin(self, agents_env, stub_core):
        await _place_worker(agents_env)
        await placement.set_attached("leaf-1", True)
        await placement.relay_admin("relocate_self", {}, as_="leaf-1")
        # A human detaches mid-session → the gate re-locks on the next call.
        await placement.set_attached("leaf-1", False)
        with pytest.raises(placement.PlacementError):
            await placement.relay_admin("relocate_self", {}, as_="leaf-1")

    async def test_unknown_admin_op_rejected(self, agents_env, stub_core):
        await _place_worker(agents_env)
        with pytest.raises(placement.PlacementError):
            await placement.relay_admin("nope", {}, as_="leaf-1")
