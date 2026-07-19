"""Stall watchdog (T5) — a live placement silent past the window is failed typed
transient and re-placed; immune while attached or paused.

The watchdog is the liveness net for a HUNG (not crashed) placement: its
subprocess is alive but emits no harness events, so ``reclaim_if_dead`` (which
fires on process exit) never triggers. A ~60s sweep measures the monotonic gap
since the last harness event (stamped in ``_reader_loop``) and, past
``AWM_PLACEMENT_STALL_S``, fails it transient (so the orchestrator's retry
routing re-places it) and kills it — reusing the close-row-then-kill ordering.
"""

from __future__ import annotations

import json
import time

import pytest

pytestmark = [pytest.mark.agent, pytest.mark.smoke]

from awm.agents import placement
from awm.agents import orch_client
from awm.agents import agent_instances as ai
from awm.agents.dao import AgentsDAO
from awm.agents._time import now_ms


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
    # Isolate the in-memory session registry for the sweep.
    ai._registry_by_id.clear()
    ai._by_scope.clear()
    yield impl
    ai._registry_by_id.clear()
    ai._by_scope.clear()
    orch_client.reset_impl()


class _FakeSession:
    """Minimal placed session the sweep iterates (mirrors AgentInstance's
    watchdog-relevant surface)."""

    def __init__(self, *, iid, token, last_activity, scope="leaf-1"):
        self.id = iid
        self.placement_token = token
        self.task_ref = "T-1"
        self.agent_ref = "agt-1"
        self.scope = scope
        self.agent_cli = "claude"
        self.last_activity = last_activity


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
        "attached": attached, "paused": paused, "intent": "live",
    })
    return iid


def _data(iid):
    return json.loads(AgentsDAO().get_instance(iid)["data"])


def _register(session):
    ai._registry_by_id[session.id] = session


async def test_fires_past_threshold(agents_env, fake_orch, monkeypatch):
    monkeypatch.delenv("AWM_PLACEMENT_STALL_S", raising=False)  # default 1800
    iid = _seed(agents_env)
    # Silent for 5000s ≫ the 1800s default → stalled.
    s = _FakeSession(iid=iid, token="plt-1", last_activity=time.monotonic() - 5000)
    _register(s)

    n = await placement._stall_sweep_once()
    assert n == 1
    # Failed TYPED transient (the same reason_type reclaim_if_dead uses → the
    # orchestrator re-places the leg within the retry cap).
    assert fake_orch.calls[0][0] == "fail"
    assert fake_orch.calls[0][1]["reason_type"] == "transient-error"
    assert "stalled" in fake_orch.calls[0][1]["reason_text"]
    # Close-row-then-kill reuse: the placement row is closed + the unit retained.
    assert _data(iid)["placement_outcome"] == "failed"
    assert ("workspace", "workspace_retain") in [
        (c[0], c[1]) for c in agents_env["calls"]]


async def test_immune_while_attached(agents_env, fake_orch, monkeypatch):
    monkeypatch.setenv("AWM_PLACEMENT_STALL_S", "1")
    iid = _seed(agents_env, attached=True)
    s = _FakeSession(iid=iid, token="plt-1", last_activity=time.monotonic() - 5000)
    _register(s)

    n = await placement._stall_sweep_once()
    assert n == 0                                   # a frozen steering session is silent by design
    assert not fake_orch.calls
    assert "placement_outcome" not in _data(iid)


async def test_immune_while_paused(agents_env, fake_orch, monkeypatch):
    monkeypatch.setenv("AWM_PLACEMENT_STALL_S", "1")
    iid = _seed(agents_env, paused=True)
    s = _FakeSession(iid=iid, token="plt-1", last_activity=time.monotonic() - 5000)
    _register(s)

    n = await placement._stall_sweep_once()
    assert n == 0
    assert not fake_orch.calls


async def test_recent_activity_not_stalled(agents_env, fake_orch, monkeypatch):
    monkeypatch.delenv("AWM_PLACEMENT_STALL_S", raising=False)
    iid = _seed(agents_env)
    s = _FakeSession(iid=iid, token="plt-1", last_activity=time.monotonic())  # just now
    _register(s)

    n = await placement._stall_sweep_once()
    assert n == 0
    assert not fake_orch.calls


async def test_closed_placement_skipped(agents_env, fake_orch, monkeypatch):
    monkeypatch.setenv("AWM_PLACEMENT_STALL_S", "1")
    iid = _seed(agents_env)
    AgentsDAO().close_placement(iid, outcome="delivered")
    s = _FakeSession(iid=iid, token="plt-1", last_activity=time.monotonic() - 5000)
    _register(s)

    n = await placement._stall_sweep_once()
    assert n == 0                                   # already closed — nothing routable
    assert not fake_orch.calls


async def test_threshold_env_is_read_at_use(agents_env, fake_orch, monkeypatch):
    # A tiny window (the live-drill lever) makes even a short gap stall.
    monkeypatch.setenv("AWM_PLACEMENT_STALL_S", "1")
    iid = _seed(agents_env)
    s = _FakeSession(iid=iid, token="plt-1", last_activity=time.monotonic() - 5)
    _register(s)

    assert await placement._stall_sweep_once() == 1
    assert fake_orch.calls[0][0] == "fail"


class TestClockStamp:
    async def test_last_activity_starts_at_spawn(self):
        # The clock is seeded at construction (placement), so a slow first event
        # never trips the watchdog.
        from awm.agents.agent_instances import AgentInstance
        before = time.monotonic()

        class _CoreStub:
            _proc = None
        inst = AgentInstance(id=1, scope="s", agent_cli="claude",
                             log_path=__import__("pathlib").Path("/tmp/x.log"),
                             agent_session=_CoreStub())
        assert inst.last_activity >= before

    async def test_reader_loop_stamps_on_event(self, agents_env, monkeypatch):
        # Every harness event advances the clock (the single chokepoint).
        from awm.agents import agent_transcript, agent_bus
        monkeypatch.setattr(agent_transcript, "record_event", lambda *a, **k: None)
        monkeypatch.setattr(agent_bus, "publish_act", lambda *a, **k: None)

        class _Ev:
            data = {}
            kind = "status"          # non-init → falls through to record_event
            text = ""
            id = "e1"

        async def _stream():
            yield _Ev()

        class _S:
            pass
        s = _S()
        s.last_activity = time.monotonic() - 1000     # stale
        await ai._reader_loop(s, _stream())
        assert s.last_activity > time.monotonic() - 5  # freshly stamped
