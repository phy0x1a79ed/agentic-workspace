"""Supervision driver — per-mode acceptance at the stop boundary, hard turn
budget, attach-freeze, liveness reclaim."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

pytestmark = [pytest.mark.agent, pytest.mark.smoke]

from awm.agents import placement
from awm.agents import orch_client
from awm.agents.agent_instances import TASK_TURN_BUDGET, TASK_WARN_REMAINING
from awm.agents.dao import AgentsDAO
from awm.agents._time import now_ms


class FakeOrch:
    def __init__(self):
        self.calls = []

    def _rec(self, n, kw):
        self.calls.append((n, kw)); return {"ack": n}

    async def deliver(self, **kw): return self._rec("deliver", kw)
    async def fail(self, **kw): return self._rec("fail", kw)
    async def decompose_commit(self, **kw): return self._rec("decompose_commit", kw)
    async def approve_plan(self, **kw): return self._rec("approve_plan", kw)
    async def reject_plan(self, **kw): return self._rec("reject_plan", kw)
    async def set_attached(self, **kw): return self._rec("set_attached", kw)


@pytest.fixture
def fake_orch(agents_env):
    impl = FakeOrch()
    orch_client.set_impl(impl)
    yield impl
    orch_client.reset_impl()


class _FakeSup:
    """Minimal task-bound session for the supervision driver."""

    def __init__(self, *, iid, token, turn_budget, mode="worker"):
        self.id = iid
        self.placement_token = token
        self.task_ref = "T-1"
        self.agent_ref = "agt-1"
        self.project = "p"
        self.scope = "leaf-1"
        self.mode = mode
        self.turn_budget = turn_budget
        self.input_queue = asyncio.Queue(maxsize=128)


def _seed(agents_env, *, token="plt-1", mode="worker", contracts_out=("out:1",),
          done=False, staged=None, graph=None, attached=False, intent="live"):
    dao = AgentsDAO()
    iid = dao.open_task_instance(
        project="p", scope="leaf-1", log_path=None, cli_session_id=None,
        started_at=now_ms(), mode=mode, task_ref="T-1", agent_ref="agt-1",
        placement_token=token)
    wp = agents_env["awm_dir"] / "services" / "workspace" / "units" / "p" / "leaf-1"
    (wp / "deliverable").mkdir(parents=True, exist_ok=True)
    patch = {
        "placement": {"mode": mode, "workspace_path": str(wp),
                      "contracts_in": [], "contracts_out": list(contracts_out),
                      "brief": ""},
        "staged": staged or {}, "done": done,
        "graph": graph or {"subtasks": [], "dependencies": [], "contracts": []},
        "attached": attached, "intent": intent,
    }
    dao.merge_instance_data(iid, patch)
    return iid, str(wp)


def _stage(wp, contract, content="x"):
    p = Path(wp) / "deliverable" / contract / "payload"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    return f"deliverable/{contract}/payload"


def _data(iid):
    return json.loads(AgentsDAO().get_instance(iid)["data"])


class TestTurnBudget:
    async def test_decrements_and_nudges_when_not_done(self, agents_env, fake_orch):
        iid, _ = _seed(agents_env)
        s = _FakeSup(iid=iid, token="plt-1", turn_budget=TASK_TURN_BUDGET)
        await placement.on_turn_boundary(s)
        assert s.turn_budget == TASK_TURN_BUDGET - 1
        author, body = s.input_queue.get_nowait()
        assert author == "supervisor" and "Continue task" in body
        assert "edit_deliverable" in body          # worker-mode hint
        assert not fake_orch.calls

    async def test_warning_in_final_stretch(self, agents_env, fake_orch):
        iid, _ = _seed(agents_env)
        s = _FakeSup(iid=iid, token="plt-1", turn_budget=TASK_WARN_REMAINING + 1)
        await placement.on_turn_boundary(s)
        _a, body = s.input_queue.get_nowait()
        assert "force-failed" in body and "Checkpoint" in body

    async def test_force_fail_at_zero_retains(self, agents_env, fake_orch):
        iid, _ = _seed(agents_env)
        s = _FakeSup(iid=iid, token="plt-1", turn_budget=1)
        await placement.on_turn_boundary(s)
        assert s.turn_budget == 0
        assert fake_orch.calls[0][0] == "fail"
        assert fake_orch.calls[0][1]["reason_type"] == "transient-error"
        assert "failed" in AgentsDAO().get_instance(iid)["data"]
        assert ("workspace", "workspace_retain") in [
            (c[0], c[1]) for c in agents_env["calls"]]

    async def test_noop_when_placement_closed(self, agents_env, fake_orch):
        iid, _ = _seed(agents_env)
        AgentsDAO().close_placement(iid, outcome="delivered")
        s = _FakeSup(iid=iid, token="plt-1", turn_budget=50)
        await placement.on_turn_boundary(s)
        assert s.turn_budget == 50
        assert s.input_queue.empty()
        assert not fake_orch.calls


class TestAcceptance:
    async def test_worker_accepts_and_delivers(self, agents_env, fake_orch):
        iid, wp = _seed(agents_env, done=True)
        _stage(wp, "out:1")
        AgentsDAO().merge_instance_data(
            iid, {"staged": {"out:1": "deliverable/out:1/payload"}})
        s = _FakeSup(iid=iid, token="plt-1", turn_budget=50)
        await placement.on_turn_boundary(s)
        # Accepted: delivered + closed + retained, budget NOT spent, no nudge.
        assert ("deliver", {"task_id": "T-1", "agent_ref": "agt-1",
                            "contract": "out:1",
                            "payload_ref": str(Path(wp) / "deliverable/out:1/payload")}
                ) in fake_orch.calls
        assert "delivered" in _data(iid).get("placement_outcome", "") or \
            _data(iid)["placement_outcome"] == "delivered"
        assert s.turn_budget == 50
        assert s.input_queue.empty()

    async def test_done_but_missing_output_nudges_not_delivers(
            self, agents_env, fake_orch):
        iid, _ = _seed(agents_env, done=True)  # marked done but nothing staged
        s = _FakeSup(iid=iid, token="plt-1", turn_budget=50)
        await placement.on_turn_boundary(s)
        assert not any(c[0] == "deliver" for c in fake_orch.calls)
        assert s.turn_budget == 49                 # fell through to budget
        assert not s.input_queue.empty()

    async def test_planner_commits_funnel_graph(self, agents_env, fake_orch):
        # a -> b: a produces cA (which b consumes), b produces cB (the terminal
        # sink). The agents-native buffer is translated to the orchestrator's
        # decompose_commit shape before the B-op fires.
        graph = {"subtasks": [{"id": "a", "objective": "first",
                               "contracts_out": ["cA"]},
                              {"id": "b", "objective": "second",
                               "contracts_out": ["cB"]}],
                 "dependencies": [{"from": "a", "to": "b"}],
                 "contracts": [{"name": "cA", "spec": "A out"},
                               {"name": "cB", "spec": "B out"}]}
        iid, _ = _seed(agents_env, token="plt-pl", mode="planner",
                       done=True, graph=graph)
        s = _FakeSup(iid=iid, token="plt-pl", turn_budget=50, mode="planner")
        await placement.on_turn_boundary(s)
        commit = next(c for c in fake_orch.calls if c[0] == "decompose_commit")
        kw = commit[1]
        # Translated to the orchestrator-native shape.
        assert kw["children"] == [{"ref": "a", "goal": "first"},
                                  {"ref": "b", "goal": "second"}]
        assert {"name": "cA", "spec": "A out", "producer": "a"} in kw["contracts"]
        assert kw["edges"] == [{"consumer": "b", "contract": "cA"}]
        assert _data(iid)["placement_outcome"] == "decomposed"
        assert s.turn_budget == 50

    async def test_planner_malformed_graph_corrected_not_committed(
            self, agents_env, fake_orch):
        graph = {"subtasks": [{"id": "a"}, {"id": "b"}],  # two sinks
                 "dependencies": [], "contracts": []}
        iid, _ = _seed(agents_env, token="plt-pl", mode="planner",
                       done=True, graph=graph)
        s = _FakeSup(iid=iid, token="plt-pl", turn_budget=50, mode="planner")
        await placement.on_turn_boundary(s)
        assert not any(c[0] == "decompose_commit" for c in fake_orch.calls)
        assert _data(iid)["done"] is False          # done cleared
        _a, body = s.input_queue.get_nowait()
        assert "funnel" in body

    async def test_verify_has_no_auto_accept(self, agents_env, fake_orch):
        iid, _ = _seed(agents_env, token="plt-v", mode="verify",
                       contracts_out=[])
        s = _FakeSup(iid=iid, token="plt-v", turn_budget=50, mode="verify")
        await placement.on_turn_boundary(s)
        # No deliver/approve auto-fired; just nudged toward a verdict.
        assert not fake_orch.calls
        _a, body = s.input_queue.get_nowait()
        assert "approve_plan" in body


class TestAttachIsPassive:
    async def test_attached_does_not_freeze_supervisor(self, agents_env, fake_orch):
        # T2: attach is passive — the agent keeps working autonomously whether or
        # not a human is attached. The budget still decrements and the supervisor
        # still nudges; attach is only a latent orchestrator hint, not a gate.
        iid, _ = _seed(agents_env, attached=True)
        s = _FakeSup(iid=iid, token="plt-1", turn_budget=50)
        await placement.on_turn_boundary(s)
        assert s.turn_budget == 49                  # decremented as usual
        author, body = s.input_queue.get_nowait()   # continuation injected
        assert author == "supervisor" and "Continue task" in body


class TestLiveness:
    async def test_dead_subprocess_fails_open_placement(self, agents_env, fake_orch):
        iid, _ = _seed(agents_env, intent="live")
        s = _FakeSup(iid=iid, token="plt-1", turn_budget=50)
        await placement.reclaim_if_dead(s)
        assert fake_orch.calls[0][0] == "fail"
        assert _data(iid)["placement_outcome"] == "failed"
        assert ("workspace", "workspace_retain") in [
            (c[0], c[1]) for c in agents_env["calls"]]

    async def test_intentional_stop_not_reclaimed(self, agents_env, fake_orch):
        iid, _ = _seed(agents_env, intent="stopped")
        s = _FakeSup(iid=iid, token="plt-1", turn_budget=50)
        await placement.reclaim_if_dead(s)
        assert not fake_orch.calls                  # respawn/stop, not a crash

    async def test_already_closed_not_reclaimed(self, agents_env, fake_orch):
        iid, _ = _seed(agents_env)
        AgentsDAO().close_placement(iid, outcome="delivered")
        s = _FakeSup(iid=iid, token="plt-1", turn_budget=50)
        await placement.reclaim_if_dead(s)
        assert not fake_orch.calls
