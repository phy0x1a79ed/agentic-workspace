"""Placement tool relays — staging, verdicts, planner buffer, token resolution,
resolved refs, retain-after-ack ordering, funnel check."""

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
    """Records B-op calls into the shared agents_env log so ordering vs
    workspace_retain is observable; returns a recognizable ack."""

    def __init__(self, log):
        self.log = log

    def _rec(self, name, kw):
        self.log.append(("orch", name, kw))
        return {"ack": name}

    async def deliver(self, **kw): return self._rec("deliver", kw)
    async def fail(self, **kw): return self._rec("fail", kw)
    async def decompose_commit(self, **kw): return self._rec("decompose_commit", kw)
    async def approve_plan(self, **kw): return self._rec("approve_plan", kw)
    async def reject_plan(self, **kw): return self._rec("reject_plan", kw)
    async def set_attached(self, **kw): return self._rec("set_attached", kw)
    async def search_tasks(self, **kw): return {"tasks": []}
    async def search_contracts(self, **kw): return {"contracts": []}


@pytest.fixture
def fake_orch(agents_env):
    impl = FakeOrch(agents_env["calls"])
    orch_client.set_impl(impl)
    yield impl
    orch_client.reset_impl()


def _seed(agents_env, *, token="plt-1", task="T-1", agent="agt-1", mode="worker",
          project="p", scope="leaf-1", contracts_out=("out:1",)):
    """Insert an open placement row + lay out its workspace unit, with the
    placement spec on the row's data (what the relays resolve against)."""
    dao = AgentsDAO()
    iid = dao.open_task_instance(
        project=project, scope=scope, log_path=None, cli_session_id=None,
        started_at=now_ms(), mode=mode, task_ref=task, agent_ref=agent,
        placement_token=token)
    wp = (agents_env["awm_dir"] / "services" / "workspace" / "units"
          / project / scope)
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


class TestTokenResolution:
    async def test_unknown_token_rejected(self, agents_env, fake_orch):
        with pytest.raises(placement.PlacementError):
            await placement.relay_indicate_done({"placement_token": "nope"})

    async def test_missing_token_rejected(self, agents_env, fake_orch):
        with pytest.raises(placement.PlacementError):
            await placement.relay_indicate_done({})

    async def test_closed_placement_rejected(self, agents_env, fake_orch):
        iid, _ = _seed(agents_env)
        AgentsDAO().close_placement(iid, outcome="delivered")
        with pytest.raises(placement.PlacementError):
            await placement.relay_indicate_done({"placement_token": "plt-1"})


class TestDeliverableStaging:
    async def test_edit_writes_payload_and_tracks_staged(self, agents_env, fake_orch):
        iid, wp = _seed(agents_env)
        await placement.relay_edit_deliverable({
            "placement_token": "plt-1", "contract": "out:1", "content": "hello"})
        assert (Path(wp) / "deliverable" / "out:1" / "payload").read_text() == "hello"
        assert _data(iid)["staged"] == {"out:1": "deliverable/out:1/payload"}

    async def test_indicate_done_sets_flag(self, agents_env, fake_orch):
        iid, _ = _seed(agents_env)
        await placement.relay_indicate_done({"placement_token": "plt-1"})
        assert _data(iid)["done"] is True

    async def test_edit_after_done_clears_done(self, agents_env, fake_orch):
        iid, _ = _seed(agents_env)
        await placement.relay_indicate_done({"placement_token": "plt-1"})
        await placement.relay_edit_deliverable({
            "placement_token": "plt-1", "contract": "out:1", "content": "v2"})
        assert _data(iid)["done"] is False


class TestTaskFail:
    async def test_fail_uses_resolved_refs_and_retains_after_ack(
            self, agents_env, fake_orch):
        _seed(agents_env, token="plt-f", task="T-real", agent="agt-real")
        await placement.relay_task_fail({
            "placement_token": "plt-f", "reason_type": "blocked",
            "reason_text": "x", "partial_ref": "art:p",
            "task_id": "SPOOF", "agent_ref": "SPOOF"})
        log = agents_env["calls"]
        _o, _n, kw = next(c for c in log if c[1] == "fail")
        assert kw["task_id"] == "T-real" and kw["agent_ref"] == "agt-real"
        assert kw["partial_ref"] == "art:p"
        names = [(c[0], c[1]) for c in log]
        # retain happens AFTER the orchestrator ack, and it's retain (not delete).
        assert names.index(("orch", "fail")) < names.index(
            ("workspace", "workspace_retain"))


class TestVerifierVerdicts:
    async def test_approve_relays_and_retains(self, agents_env, fake_orch):
        iid, _ = _seed(agents_env, token="plt-v", mode="verify")
        res = await placement.relay_approve_plan({"placement_token": "plt-v"})
        assert res["outcome"] == "approved"
        names = [(c[0], c[1]) for c in agents_env["calls"]]
        assert ("orch", "approve_plan") in names
        assert ("workspace", "workspace_retain") in names
        assert "approved" in AgentsDAO().get_instance(iid)["data"]

    async def test_reject_relays_reason(self, agents_env, fake_orch):
        _seed(agents_env, token="plt-r", mode="verify")
        await placement.relay_reject_plan({
            "placement_token": "plt-r", "reason": "missing tests"})
        _o, _n, kw = next(c for c in agents_env["calls"] if c[1] == "reject_plan")
        assert kw["reason"] == "missing tests"


class TestPlannerBuffer:
    async def test_add_subtask_and_dependency_buffer(self, agents_env, fake_orch):
        iid, _ = _seed(agents_env, token="plt-pl", mode="planner")
        await placement.relay_add_subtask({
            "placement_token": "plt-pl", "id": "a", "objective": "first"})
        await placement.relay_add_subtask({
            "placement_token": "plt-pl", "id": "b", "objective": "second"})
        await placement.relay_add_dependency({
            "placement_token": "plt-pl", "from_id": "a", "to_id": "b"})
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
