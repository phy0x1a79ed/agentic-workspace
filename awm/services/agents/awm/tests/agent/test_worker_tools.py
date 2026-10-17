"""Placement tool relays (agent domain) — deliverable staging, the planner graph
buffer, identity resolution, and the funnel check.

The worker VERDICTS (approve/reject/accept), the escape hatch (task_fail), and
the planner READS moved to the orchestrator's ``orchwork`` domain (the lift,
T4a); their coverage lives in the orchestrator's own ``test_worker_tools``. What
stays here is the STATE-bearing surface the agents service still hosts."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytestmark = [pytest.mark.agent, pytest.mark.smoke]

from awm.agents import placement
from awm.agents import orch_client
from awm.agents.dao import AgentsDAO
from awm.agents._time import now_ms


class FakeOrch:
    """Records the (shrunk, T4a) contract-B calls into the shared agents_env log
    so ordering vs workspace_retain is observable; returns a recognizable ack.

    The seam shrank with the lift: only the SUPERVISION outcomes + side-channel
    mirrors remain on it (the verdicts / task_fail / planner reads are orchwork
    now, driven by the orchestrator, not relayed from here)."""

    def __init__(self, log):
        self.log = log

    def _rec(self, name, kw):
        self.log.append(("orch", name, kw))
        return {"ack": name}

    async def deliver(self, **kw): return self._rec("deliver", kw)
    async def fail(self, **kw): return self._rec("fail", kw)
    async def decompose_commit(self, **kw): return self._rec("decompose_commit", kw)
    async def set_attached(self, **kw): return self._rec("set_attached", kw)
    async def set_steering(self, **kw): return self._rec("set_steering", kw)
    async def set_paused(self, **kw): return self._rec("set_paused", kw)


@pytest.fixture
def fake_orch(agents_env):
    impl = FakeOrch(agents_env["calls"])
    orch_client.set_impl(impl)
    yield impl
    orch_client.reset_impl()


def _seed(agents_env, *, token="plt-1", task="T-1", agent="agt-1", mode="worker",
          scope="leaf-1", contracts_out=("out:1",)):
    """Insert an open placement row + lay out its workspace unit, with the
    placement spec on the row's data (what the relays resolve against)."""
    dao = AgentsDAO()
    iid = dao.open_task_instance(
        scope=scope, log_path=None, cli_session_id=None,
        started_at=now_ms(), mode=mode, task_ref=task, agent_ref=agent,
        placement_token=token)
    wp = (agents_env["awm_dir"] / "services" / "workspace" / "units" / scope)
    (wp / "deliverable").mkdir(parents=True, exist_ok=True)
    dao.merge_instance_data(iid, {
        "placement": {"mode": mode, "workspace_path": str(wp),
                      "contracts_in": [], "contracts_out": list(contracts_out),
                      "brief": ""},
        "staged": {}, "done": False,
        "graph": {"subtasks": [], "dependencies": [], "contracts": []},
        "attached": False,
    })
    return iid, str(wp)


def _data(iid):
    return json.loads(AgentsDAO().get_instance(iid)["data"])


# The placement's identity is the scope (the unit slug) — what the per-placement
# AWM_AS carries and the relays resolve against (the second positional `as_`).
_AS = "leaf-1"


class TestIdentityResolution:
    async def test_unknown_identity_rejected(self, agents_env, fake_orch):
        with pytest.raises(placement.PlacementError):
            await placement.relay_indicate_done({}, "does-not-exist")

    async def test_missing_identity_rejected(self, agents_env, fake_orch):
        with pytest.raises(placement.PlacementError):
            await placement.relay_indicate_done({}, None)

    async def test_closed_placement_rejected(self, agents_env, fake_orch):
        iid, _ = _seed(agents_env)
        AgentsDAO().close_placement(iid, outcome="delivered")
        with pytest.raises(placement.PlacementError):
            await placement.relay_indicate_done({}, _AS)


class TestDeliverableStaging:
    async def test_edit_writes_payload_and_tracks_staged(self, agents_env, fake_orch):
        iid, wp = _seed(agents_env)
        await placement.relay_edit_deliverable({
            "contract": "out:1", "content": "hello"}, _AS)
        assert (Path(wp) / "deliverable" / "out:1" / "payload").read_text() == "hello"
        assert _data(iid)["staged"] == {"out:1": "deliverable/out:1/payload"}

    async def test_indicate_done_sets_flag(self, agents_env, fake_orch):
        iid, _ = _seed(agents_env)
        await placement.relay_indicate_done({}, _AS)
        assert _data(iid)["done"] is True

    async def test_edit_after_done_clears_done(self, agents_env, fake_orch):
        iid, _ = _seed(agents_env)
        await placement.relay_indicate_done({}, _AS)
        await placement.relay_edit_deliverable({
            "contract": "out:1", "content": "v2"}, _AS)
        assert _data(iid)["done"] is False


class TestPlannerBuffer:
    async def test_add_subtask_and_dependency_buffer(self, agents_env, fake_orch):
        iid, _ = _seed(agents_env, token="plt-pl", mode="planner")
        await placement.relay_add_subtask({"id": "a", "objective": "first"}, _AS)
        await placement.relay_add_subtask({"id": "b", "objective": "second"}, _AS)
        await placement.relay_add_dependency({"from_id": "a", "to_id": "b"}, _AS)
        g = _data(iid)["graph"]
        assert [s["id"] for s in g["subtasks"]] == ["a", "b"]
        assert g["dependencies"] == [{"from": "a", "to": "b"}]
        # Buffering does NOT commit — no decompose_commit yet.
        assert not any(c[1] == "decompose_commit" for c in agents_env["calls"])


class TestFunnelCheck:
    def test_chain_funnels(self):
        subs = [{"id": "a"}, {"id": "b"}, {"id": "c"}]
        deps = [{"from": "a", "to": "c"}, {"from": "b", "to": "c"}]
        assert placement.funnels(subs, deps) is True

    def test_multi_sink_does_not_funnel(self):
        subs = [{"id": "a"}, {"id": "b"}]
        assert placement.funnels(subs, []) is False  # two sinks

    def test_cycle_rejected(self):
        subs = [{"id": "a"}, {"id": "b"}]
        deps = [{"from": "a", "to": "b"}, {"from": "b", "to": "a"}]
        assert placement.funnels(subs, deps) is False

    def test_empty_rejected(self):
        assert placement.funnels([], []) is False
