"""In-process lifecycle e2e — PLANNING → VERIFYING_PLAN → ACTIVE on one unit.

Ties the real pieces together without a live hub or a real claude subprocess:
``place_on_task`` (per mode) + the tool relays the agent would call + the
``on_turn_boundary`` supervisor + a stubbed orchestrator. Drives a mock task
through plan → verify-approve → active → deliver on a SINGLE reused workspace
unit, asserting the orchestrator B-ops fire in the right order. This is the
sandboxed mock-task run, minus the subprocess. (A full live-hub run with real
claude is the next-session scaffolding — see test_live_hub_e2e.py.)
"""

from __future__ import annotations

import asyncio

import pytest

pytestmark = [pytest.mark.agent, pytest.mark.smoke, pytest.mark.integration]

import awm.agents.agent_instances as ai_mod
from awm.agents import placement
from awm.agents import orch_client


class FakeOrch:
    def __init__(self):
        self.ops = []

    def _rec(self, n, kw):
        self.ops.append((n, kw)); return {"ack": n}

    async def deliver(self, **kw): return self._rec("deliver", kw)
    async def fail(self, **kw): return self._rec("fail", kw)
    async def decompose_commit(self, **kw): return self._rec("decompose_commit", kw)
    async def approve_plan(self, **kw): return self._rec("approve_plan", kw)
    async def reject_plan(self, **kw): return self._rec("reject_plan", kw)
    async def set_attached(self, **kw): return self._rec("set_attached", kw)
    async def set_steering(self, **kw): return self._rec("set_steering", kw)
    async def set_paused(self, **kw): return self._rec("set_paused", kw)


@pytest.fixture
def fake_orch(agents_env):
    impl = FakeOrch()
    orch_client.set_impl(impl)
    yield impl
    orch_client.reset_impl()


async def _await_deregister(scope, tries=60):
    """Wait for a retired placement's session to leave the registry (the fake
    proc resolves wait() on terminate(), so the waiter loop clears the scope)."""
    for _ in range(tries):
        if ai_mod.get_session_by_scope(scope) is None:
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"session for {scope} never deregistered")


async def _place(mode, *, unit="leaf-1", brief="ship X", contracts_out=None):
    args = {"task_id": "T-1", "unit_slug": unit,
            "brief": brief, "mode": mode}
    if contracts_out is not None:
        args["contracts_out"] = contracts_out
    return await placement.place_on_task(args)


class TestLifecycle:
    async def test_plan_verify_active_on_one_unit(
            self, agents_env, stub_core, fake_orch):
        UNIT = "leaf-1"

        AS = UNIT  # the placement identity the agent's proxy stamps (the slug)

        # --- PLANNING: plan-worker stages the reserved "plan" deliverable ---
        res = await _place("plan", unit=UNIT)
        await placement.relay_edit_deliverable({
            "contract": "plan",
            "content": "# Plan\n1. do the work\n2. produce out:1"}, AS)
        await placement.relay_indicate_done({}, AS)
        # Stop boundary: accept → orch.deliver("plan") → PLANNING done.
        await placement.on_turn_boundary(ai_mod.get_session_by_scope(UNIT))
        assert ("deliver", {"task_id": "T-1", "agent_ref": res["agent_ref"],
                            "contract": "plan",
                            "payload_ref": _plan_path(agents_env)}) in fake_orch.ops
        await _await_deregister(UNIT)

        # --- VERIFYING_PLAN: verifier (same unit) sees the plan, approves ---
        resv = await _place("verify", unit=UNIT)
        await asyncio.sleep(0.05)
        kickoff = "\n".join(stub_core["session"].sent)
        assert "PLAN VERIFIER" in kickoff and "do the work" in kickoff  # plan embedded
        await placement.relay_approve_plan({}, AS)
        assert any(op == "approve_plan" for op, _ in fake_orch.ops)
        await _await_deregister(UNIT)

        # --- ACTIVE: worker (same unit) stages out:1 and delivers ---
        resw = await _place("worker", unit=UNIT, contracts_out=["out:1"])
        await placement.relay_edit_deliverable({
            "contract": "out:1", "content": "the result"}, AS)
        await placement.relay_indicate_done({}, AS)
        await placement.on_turn_boundary(ai_mod.get_session_by_scope(UNIT))

        delivered = [kw for op, kw in fake_orch.ops
                     if op == "deliver" and kw["contract"] == "out:1"]
        assert delivered and delivered[0]["agent_ref"] == resw["agent_ref"]
        # Whole lifecycle reused ONE unit — only workspace_create/retain touched,
        # never the scopes service.
        svcs = {svc for (svc, _fn, _a) in agents_env["calls"]}
        assert "scopes" not in svcs
        assert "workspace" in svcs


def _plan_path(agents_env):
    return str(agents_env["awm_dir"] / "services" / "workspace" / "units"
              / "leaf-1" / "deliverable" / "plan" / "payload")
