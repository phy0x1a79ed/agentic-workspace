"""Steering protocol (T1) — agents side.

Durable attach is the single source of truth (viewing a transcript never
attaches); detach is a two-consent handshake that fires automatically when the
agent's ``request_detach`` (with the refreshed objective record) and the user's
``set_user_done`` both land. Exercises:

* the user-side by-slug ``attach`` (durable attach + clears wants-steering),
* the placed-agent relays ``request_detach`` / ``rescind_detach`` /
  ``request_steering`` (attach gating, objective write),
* the handshake auto-transition in BOTH orders,
* consent-clear on a new user post,
* freeze keyed on the durable attached flag, incl. the boundary handshake,
* re-placement coming back attended (kickoff suppressed, objective seeded).
"""

from __future__ import annotations

import asyncio
import json
import types
from pathlib import Path

import pytest

pytestmark = [pytest.mark.agent, pytest.mark.smoke]

from awm.agents import placement
from awm.agents import agent_instances as ai
from awm.agents import orch_client
from awm.agents.dao import AgentsDAO
from awm.agents._time import now_ms


class FakeOrch:
    """Records every B-op (incl. set_steering) so the mirror is observable."""

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
    async def set_steering(self, **kw): return self._rec("set_steering", kw)
    async def set_paused(self, **kw): return self._rec("set_paused", kw)

    def steering(self):
        return [kw for n, kw in self.calls if n == "set_steering"]


@pytest.fixture
def fake_orch(agents_env):
    impl = FakeOrch()
    orch_client.set_impl(impl)
    yield impl
    orch_client.reset_impl()


@pytest.fixture(autouse=True)
def _clear_registry():
    """Tests here register fake in-memory sessions in ``_by_scope`` (so by-slug
    verbs resolve); clear it after each so nothing leaks across tests."""
    yield
    ai._by_scope.clear()
    ai._registry_by_id.clear()


_AS = "leaf-1"


class TestProfilesAndManifest:
    def test_steering_verbs_in_worker_plan_planner_not_verify(self):
        for mode in ("worker", "plan", "planner"):
            assert {"request_detach", "rescind_detach", "request_steering"} <= \
                placement.VERB_PROFILES[mode]
        # verify stays verdict-only.
        assert not ({"request_detach", "rescind_detach", "request_steering"}
                    & placement.VERB_PROFILES["verify"])

    def test_new_verbs_manifested_and_handled(self):
        from awm.agents import hub_adapter
        tools = {f.get("tool") or f["name"]
                 for f in hub_adapter.API_MANIFEST["functions"]}
        for verb in ("attach", "set_user_done", "request_detach",
                     "rescind_detach", "request_steering"):
            assert f"agent_{verb}" in tools
            assert verb in hub_adapter.HANDLERS


def _seed(agents_env, *, token="plt-1", scope="leaf-1", mode="worker",
          attached=False, steer_user_done=False, steer_agent_ready=False,
          steer_requested=False, objective="", register_session=False):
    """Insert an open placement row (+ optionally a live in-memory session so the
    by-slug verbs, which resolve via ``get_session_by_scope``, can find it)."""
    dao = AgentsDAO()
    iid = dao.open_task_instance(
        scope=scope, log_path=None, cli_session_id=None, started_at=now_ms(),
        mode=mode, task_ref="T-1", agent_ref="agt-1", placement_token=token)
    wp = agents_env["awm_dir"] / "services" / "workspace" / "units" / scope
    (wp / "deliverable").mkdir(parents=True, exist_ok=True)
    dao.merge_instance_data(iid, {
        "placement": {"mode": mode, "workspace_path": str(wp),
                      "contracts_in": [], "contracts_out": ["out:1"], "brief": ""},
        "staged": {}, "done": False,
        "graph": {"subtasks": [], "dependencies": [], "contracts": []},
        "attached": attached, "paused": False,
        "steer_user_done": steer_user_done, "steer_agent_ready": steer_agent_ready,
        "steer_requested": steer_requested, "objective": objective,
    })
    if register_session:
        ai._by_scope[scope] = types.SimpleNamespace(
            placement_token=token, scope=scope, mode=mode)
    return iid, str(wp)


def _data(iid):
    return json.loads(AgentsDAO().get_instance(iid)["data"])


# ---------------------------------------------------------------------------
# User-side by-slug: attach
# ---------------------------------------------------------------------------


class TestAttach:
    async def test_attach_sets_durable_and_clears_wants_steering(
            self, agents_env, fake_orch):
        iid, _ = _seed(agents_env, steer_requested=True, register_session=True)
        res = await placement.attach(_AS)
        assert res["attached"] is True
        d = _data(iid)
        assert d["attached"] is True and d["steer_requested"] is False
        mirror = fake_orch.steering()[-1]
        assert mirror["attached"] is True and mirror["steer_requested"] is False

    async def test_attach_by_task_id_when_no_live_session(
            self, agents_env, fake_orch):
        # No live session: attach still mirrors durably (keyed on task_id).
        await placement.attach("no-such-slug", task_id="T-77")
        mirror = fake_orch.steering()[-1]
        assert mirror["task_id"] == "T-77" and mirror["attached"] is True


# ---------------------------------------------------------------------------
# Placed-agent relays: request_detach / rescind_detach / request_steering
# ---------------------------------------------------------------------------


class TestRequestDetach:
    async def test_writes_objective_and_readiness(self, agents_env, fake_orch):
        iid, _ = _seed(agents_env, attached=True)
        res = await placement.relay_request_detach(
            {"objective": "# Objective\nship it"}, _AS)
        assert res["steer_agent_ready"] is True and res["detached"] is False
        d = _data(iid)
        assert d["objective"] == "# Objective\nship it"
        assert d["steer_agent_ready"] is True
        mirror = fake_orch.steering()[-1]
        assert mirror["objective"] == "# Objective\nship it"
        assert mirror["steer_agent_ready"] is True

    async def test_rejected_when_not_attached(self, agents_env, fake_orch):
        _seed(agents_env, attached=False)
        with pytest.raises(placement.PlacementError):
            await placement.relay_request_detach({"objective": "x"}, _AS)

    async def test_rejected_without_objective(self, agents_env, fake_orch):
        _seed(agents_env, attached=True)
        with pytest.raises(placement.PlacementError):
            await placement.relay_request_detach({"objective": "  "}, _AS)


class TestRescindDetach:
    async def test_clears_readiness_keeps_objective(self, agents_env, fake_orch):
        iid, _ = _seed(agents_env, attached=True, steer_agent_ready=True,
                       objective="the record")
        await placement.relay_rescind_detach({}, _AS)
        d = _data(iid)
        assert d["steer_agent_ready"] is False
        assert d["objective"] == "the record"     # record survives a rescind

    async def test_rejected_when_not_attached(self, agents_env, fake_orch):
        _seed(agents_env, attached=False, steer_agent_ready=True)
        with pytest.raises(placement.PlacementError):
            await placement.relay_rescind_detach({}, _AS)


class TestRequestSteering:
    async def test_allowed_while_detached(self, agents_env, fake_orch):
        iid, _ = _seed(agents_env, attached=False)
        res = await placement.relay_request_steering({}, _AS)
        assert res["steer_requested"] is True
        assert _data(iid)["steer_requested"] is True
        assert fake_orch.steering()[-1]["steer_requested"] is True


# ---------------------------------------------------------------------------
# The mutual-detach handshake — both orders
# ---------------------------------------------------------------------------


def _detached_ok(d) -> bool:
    return (d["attached"] is False and d["steer_user_done"] is False
            and d["steer_agent_ready"] is False and d["steer_requested"] is False)


class TestHandshake:
    async def test_agent_ready_then_user_done(self, agents_env, fake_orch):
        iid, _ = _seed(agents_env, attached=True, register_session=True)
        # Agent consents first (no detach yet — user hasn't).
        r1 = await placement.relay_request_detach({"objective": "obj"}, _AS)
        assert r1["detached"] is False
        assert _data(iid)["attached"] is True
        # User consents → both agree → auto-detach.
        r2 = await placement.set_user_done(_AS, True)
        assert r2["detached"] is True
        d = _data(iid)
        assert _detached_ok(d)
        assert d["objective"] == "obj"            # objective committed durably

    async def test_user_done_then_agent_ready(self, agents_env, fake_orch):
        iid, _ = _seed(agents_env, attached=True, register_session=True)
        # User consents first (agent not ready — no detach).
        r1 = await placement.set_user_done(_AS, True)
        assert r1["detached"] is False
        assert _data(iid)["attached"] is True
        # Agent consents → both agree → auto-detach.
        r2 = await placement.relay_request_detach({"objective": "obj"}, _AS)
        assert r2["detached"] is True
        assert _detached_ok(_data(iid))

    async def test_set_user_done_rejected_when_not_attached(
            self, agents_env, fake_orch):
        _seed(agents_env, attached=False, register_session=True)
        res = await placement.set_user_done(_AS, True)
        assert res["ok"] is False

    async def test_handshake_is_idempotent(self, agents_env, fake_orch):
        iid, _ = _seed(agents_env, attached=True, steer_agent_ready=True,
                       steer_user_done=True, objective="obj",
                       register_session=True)
        first = await placement._run_handshake(iid)
        second = await placement._run_handshake(iid)
        assert first is True and second is False   # only fires once
        assert _detached_ok(_data(iid))


# ---------------------------------------------------------------------------
# A new user post clears the agent's readiness (must re-confirm)
# ---------------------------------------------------------------------------


class TestConsentClearOnPost:
    async def test_new_post_clears_agent_ready(self, agents_env, fake_orch):
        iid, _ = _seed(agents_env, attached=True, steer_agent_ready=True)
        session = types.SimpleNamespace(placement_token="plt-1")
        placement.note_user_post(session)
        assert _data(iid)["steer_agent_ready"] is False
        await asyncio.sleep(0)                     # let the scheduled mirror run
        assert any(m.get("steer_agent_ready") is False
                   for m in fake_orch.steering())

    async def test_noop_when_detached(self, agents_env, fake_orch):
        iid, _ = _seed(agents_env, attached=False, steer_agent_ready=True)
        placement.note_user_post(types.SimpleNamespace(placement_token="plt-1"))
        # Detached → the readiness bit is not the agent's steering consent; leave it.
        assert _data(iid)["steer_agent_ready"] is True


# ---------------------------------------------------------------------------
# Freeze keys on the durable attached flag (incl. the boundary handshake)
# ---------------------------------------------------------------------------


class _FakeSup:
    def __init__(self, *, iid, token, turn_budget, mode="worker"):
        self.id = iid
        self.placement_token = token
        self.task_ref = "T-1"
        self.agent_ref = "agt-1"
        self.scope = "leaf-1"
        self.mode = mode
        self.turn_budget = turn_budget
        self.input_queue = asyncio.Queue(maxsize=128)


class TestFreezeAndBoundaryHandshake:
    async def test_durable_attached_freezes(self, agents_env, fake_orch):
        iid, _ = _seed(agents_env, attached=True)
        s = _FakeSup(iid=iid, token="plt-1", turn_budget=50)
        await placement.on_turn_boundary(s)
        assert s.turn_budget == 50                 # frozen

    async def test_boundary_fires_handshake_then_resumes(
            self, agents_env, fake_orch):
        # Both bits already agree at the boundary → auto-detach, and because
        # attached is now cleared the supervisor resumes on THIS boundary.
        iid, _ = _seed(agents_env, attached=True, steer_agent_ready=True,
                       steer_user_done=True, objective="obj")
        s = _FakeSup(iid=iid, token="plt-1", turn_budget=50)
        await placement.on_turn_boundary(s)
        assert _detached_ok(_data(iid))
        assert s.turn_budget == 49                 # supervision resumed


# ---------------------------------------------------------------------------
# Re-placement comes back attended: kickoff suppressed, objective seeded
# ---------------------------------------------------------------------------


def _brief(goal, objective=""):
    b = {"goal": goal, "mode": "worker"}
    if objective:
        b["objective"] = objective
    return json.dumps(b)


class TestReplacementAttended:
    async def test_attached_worker_suppresses_kickoff_and_seeds_objective(
            self, agents_env, stub_core, fake_orch):
        res = await placement.place_on_task({
            "task_id": "T-1", "unit_slug": "leaf-A",
            "brief": _brief("ship it", objective="# Objective\nthe intent"),
            "contracts_out": ["out:1"], "mode": "worker", "attached": True})
        await asyncio.sleep(0.05)
        # Kickoff suppressed — the attended worker idles for the human.
        assert not any("Begin now" in t for t in stub_core["session"].sent)
        # Objective seeded on the row + rendered into the unit CLAUDE.md.
        row = AgentsDAO().resolve_placement(res["placement_token"])
        d = json.loads(row["data"])
        assert d["attached"] is True
        assert d["objective"] == "# Objective\nthe intent"
        ctx = next(a["context_md"] for (svc, fn, a) in agents_env["calls"]
                   if fn == "workspace_create" and a.get("unit_slug") == "leaf-A")
        assert "Objective record (ratified)" in ctx
        assert "the intent" in ctx
        assert "request_detach" in ctx             # steering section present

    async def test_detached_worker_kicks_off(
            self, agents_env, stub_core, fake_orch):
        await placement.place_on_task({
            "task_id": "T-2", "unit_slug": "leaf-B",
            "brief": _brief("ship it"), "contracts_out": ["out:1"],
            "mode": "worker", "attached": False})
        await asyncio.sleep(0.05)
        assert any("Begin now" in t for t in stub_core["session"].sent)
