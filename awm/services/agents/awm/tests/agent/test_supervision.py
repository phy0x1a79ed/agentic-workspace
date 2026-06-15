"""T4: supervision driver — hard turn budget, warning window, forced fail."""

from __future__ import annotations

import asyncio

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

    async def fail(self, **kw):
        self.calls.append(("fail", kw)); return {"ack": "fail"}

    async def deliver(self, **kw):
        self.calls.append(("deliver", kw)); return {}

    async def claim(self, **kw):
        return {}

    async def decompose_commit(self, **kw):
        return {}


@pytest.fixture
def fake_orch(agents_env):
    impl = FakeOrch()
    orch_client.set_impl(impl)
    yield impl
    orch_client.reset_impl()


class _FakeSup:
    """Minimal task-bound session for the supervision driver."""

    def __init__(self, *, iid, token, turn_budget):
        self.id = iid
        self.placement_token = token
        self.task_ref = "T-1"
        self.agent_ref = "agt-1"
        self.project = "p"
        self.scope = "leaf-1"
        self.mode = "worker"
        self.turn_budget = turn_budget
        self.input_queue = asyncio.Queue(maxsize=128)


def _seed(token="plt-1", **kw):
    dao = AgentsDAO()
    return dao.open_task_instance(
        project="p", scope="leaf-1", log_path=None, cli_session_id=None,
        started_at=now_ms(), mode="worker", task_ref="T-1", agent_ref="agt-1",
        placement_token=token)


class TestTurnBudget:
    async def test_decrements_every_turn(self, agents_env, fake_orch):
        iid = _seed()
        s = _FakeSup(iid=iid, token="plt-1", turn_budget=TASK_TURN_BUDGET)
        await placement.on_turn_boundary(s)
        assert s.turn_budget == TASK_TURN_BUDGET - 1
        author, body = s.input_queue.get_nowait()
        assert author == "supervisor"
        assert "Continue task" in body
        assert not fake_orch.calls  # not failed yet

    async def test_warning_in_final_stretch(self, agents_env, fake_orch):
        iid = _seed()
        # After decrement remaining == TASK_WARN_REMAINING → warning prompt.
        s = _FakeSup(iid=iid, token="plt-1", turn_budget=TASK_WARN_REMAINING + 1)
        await placement.on_turn_boundary(s)
        _author, body = s.input_queue.get_nowait()
        assert "force-failed" in body
        assert "COMMIT" in body

    async def test_force_fail_at_zero(self, agents_env, fake_orch):
        iid = _seed()
        s = _FakeSup(iid=iid, token="plt-1", turn_budget=1)
        await placement.on_turn_boundary(s)
        assert s.turn_budget == 0
        # Forced fail relayed as transient-error.
        assert fake_orch.calls and fake_orch.calls[0][0] == "fail"
        kw = fake_orch.calls[0][1]
        assert kw["reason_type"] == "transient-error"
        # Placement closed + scope retired (scope_complete, not delete).
        assert "failed" in AgentsDAO().get_instance(iid)["data"]
        assert ("scopes", "scope_complete") in [(c[0], c[1]) for c in agents_env["calls"]]

    async def test_noop_when_placement_closed(self, agents_env, fake_orch):
        iid = _seed()
        AgentsDAO().close_placement(iid, outcome="delivered")
        s = _FakeSup(iid=iid, token="plt-1", turn_budget=50)
        await placement.on_turn_boundary(s)
        assert s.turn_budget == 50              # not decremented
        assert s.input_queue.empty()            # no continuation injected
        assert not fake_orch.calls
