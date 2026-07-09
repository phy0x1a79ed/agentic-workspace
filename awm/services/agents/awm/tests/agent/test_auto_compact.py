"""Auto-compact (T5) — at the turn boundary, AFTER the freeze check, a claude
placement past the context threshold gets a native ``/compact`` injected; the
compact IS that boundary's action (skip the budget decrement + continuation
nudge), guarded by a cooldown and never fired while attached or paused.
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest

pytestmark = [pytest.mark.agent, pytest.mark.smoke]

from awm.agents import placement
from awm.agents import orch_client
from awm.agents import agent_instances as ai
from awm.agents.dao import AgentsDAO
from awm.agents._time import now_ms
from awm.agents.agent_instances import TASK_TURN_BUDGET


class FakeOrch:
    def __init__(self):
        self.calls = []

    def _rec(self, n, kw):
        self.calls.append((n, kw)); return {"ack": n}

    async def fail(self, **kw): return self._rec("fail", kw)
    async def deliver(self, **kw): return self._rec("deliver", kw)
    async def set_steering(self, **kw): return self._rec("set_steering", kw)


@pytest.fixture
def fake_orch(agents_env):
    impl = FakeOrch()
    orch_client.set_impl(impl)
    yield impl
    orch_client.reset_impl()


@pytest.fixture
def slash_calls(monkeypatch):
    """Record every ``/compact`` (or other slash) injection instead of driving a
    real tmux session."""
    calls: list = []

    async def _fake_send_slash(scope, body):
        calls.append((scope, body))

    monkeypatch.setattr(ai, "send_slash", _fake_send_slash)
    return calls


class _FakeSession:
    def __init__(self, *, iid, token, agent_cli="claude",
                 context_used=0, context_max=None, last_compact_at=0.0,
                 turn_budget=TASK_TURN_BUDGET):
        self.id = iid
        self.placement_token = token
        self.task_ref = "T-1"
        self.agent_ref = "agt-1"
        self.scope = "leaf-1"
        self.mode = "worker"
        self.agent_cli = agent_cli
        self.context_used = context_used
        self.context_max = context_max
        self.last_compact_at = last_compact_at
        self.turn_budget = turn_budget
        self.input_queue = asyncio.Queue(maxsize=128)


def _seed(agents_env, *, token="plt-1", attached=False, paused=False):
    dao = AgentsDAO()
    iid = dao.open_task_instance(
        scope="leaf-1", log_path=None, cli_session_id=None,
        started_at=now_ms(), mode="worker", task_ref="T-1", agent_ref="agt-1",
        placement_token=token)
    wp = agents_env["awm_dir"] / "services" / "workspace" / "units" / "leaf-1"
    (wp / "deliverable").mkdir(parents=True, exist_ok=True)
    dao.merge_instance_data(iid, {
        "placement": {"mode": "worker", "workspace_path": str(wp),
                      "contracts_in": [], "contracts_out": ["out:1"], "brief": ""},
        "staged": {}, "done": False,
        "attached": attached, "paused": paused, "intent": "live",
    })
    return iid


# 200k context; 170k used = 0.85 ≥ the 0.8 default threshold.
_MAX = 200_000
_HOT = 170_000
_COLD = 100_000


async def test_fires_at_ratio_and_skips_budget_and_nudge(
        agents_env, fake_orch, slash_calls, monkeypatch):
    monkeypatch.delenv("AWM_COMPACT_THRESHOLD", raising=False)   # default 0.8
    iid = _seed(agents_env)
    s = _FakeSession(iid=iid, token="plt-1", context_used=_HOT, context_max=_MAX,
                     turn_budget=50)
    await placement.on_turn_boundary(s)
    # /compact injected via the native slash seam.
    assert slash_calls == [("leaf-1", "/compact")]
    # The compact IS this boundary's action: budget NOT spent, no nudge enqueued.
    assert s.turn_budget == 50
    assert s.input_queue.empty()
    assert not fake_orch.calls
    # Cooldown stamp recorded.
    assert s.last_compact_at > 0.0


async def test_cooldown_suppresses_refire(
        agents_env, fake_orch, slash_calls, monkeypatch):
    monkeypatch.delenv("AWM_COMPACT_THRESHOLD", raising=False)
    iid = _seed(agents_env)
    # Just compacted → still cooling down; the ratio is still hot.
    s = _FakeSession(iid=iid, token="plt-1", context_used=_HOT, context_max=_MAX,
                     last_compact_at=time.monotonic(), turn_budget=50)
    await placement.on_turn_boundary(s)
    assert slash_calls == []                    # suppressed
    assert s.turn_budget == 49                  # fell through to the budget path
    assert not s.input_queue.empty()            # nudge enqueued


async def test_below_threshold_no_compact(
        agents_env, fake_orch, slash_calls, monkeypatch):
    monkeypatch.delenv("AWM_COMPACT_THRESHOLD", raising=False)
    iid = _seed(agents_env)
    s = _FakeSession(iid=iid, token="plt-1", context_used=_COLD, context_max=_MAX,
                     turn_budget=50)
    await placement.on_turn_boundary(s)
    assert slash_calls == []
    assert s.turn_budget == 49


async def test_non_claude_harness_skipped(
        agents_env, fake_orch, slash_calls, monkeypatch):
    monkeypatch.delenv("AWM_COMPACT_THRESHOLD", raising=False)
    iid = _seed(agents_env)
    # opencode: no native /compact — the ratio is hot but the compact is skipped.
    s = _FakeSession(iid=iid, token="plt-1", agent_cli="opencode",
                     context_used=_HOT, context_max=_MAX, turn_budget=50)
    await placement.on_turn_boundary(s)
    assert slash_calls == []
    assert s.turn_budget == 49                  # normal autonomous supervision


async def test_no_context_max_skipped(
        agents_env, fake_orch, slash_calls, monkeypatch):
    monkeypatch.delenv("AWM_COMPACT_THRESHOLD", raising=False)
    iid = _seed(agents_env)
    # No context_max (no init event yet) → can't compute a ratio → skip.
    s = _FakeSession(iid=iid, token="plt-1", context_used=_HOT, context_max=None,
                     turn_budget=50)
    await placement.on_turn_boundary(s)
    assert slash_calls == []
    assert s.turn_budget == 49


async def test_never_while_attached(
        agents_env, fake_orch, slash_calls, monkeypatch):
    monkeypatch.delenv("AWM_COMPACT_THRESHOLD", raising=False)
    iid = _seed(agents_env, attached=True)
    s = _FakeSession(iid=iid, token="plt-1", context_used=_HOT, context_max=_MAX,
                     turn_budget=50)
    await placement.on_turn_boundary(s)
    # The freeze check runs BEFORE the compact — an attached node never compacts.
    assert slash_calls == []
    assert s.turn_budget == 50                  # frozen


async def test_never_while_paused(
        agents_env, fake_orch, slash_calls, monkeypatch):
    monkeypatch.delenv("AWM_COMPACT_THRESHOLD", raising=False)
    iid = _seed(agents_env, paused=True)
    s = _FakeSession(iid=iid, token="plt-1", context_used=_HOT, context_max=_MAX,
                     turn_budget=50)
    await placement.on_turn_boundary(s)
    assert slash_calls == []
    assert s.turn_budget == 50                  # frozen


async def test_threshold_env_is_read_at_use(
        agents_env, fake_orch, slash_calls, monkeypatch):
    # Lower the bar to 0.4 → a cold-ish session now compacts (env read at use).
    monkeypatch.setenv("AWM_COMPACT_THRESHOLD", "0.4")
    iid = _seed(agents_env)
    s = _FakeSession(iid=iid, token="plt-1", context_used=_COLD, context_max=_MAX,
                     turn_budget=50)
    await placement.on_turn_boundary(s)
    assert slash_calls == [("leaf-1", "/compact")]
    assert s.turn_budget == 50
