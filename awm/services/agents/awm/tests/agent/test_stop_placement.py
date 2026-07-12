"""Node lifecycle controls (T4) — the agents-side stop seam.

``stop_placement`` is the orchestrator-called kill seam (manifest-omitted,
reached via the gateway catch-all). ORDERING IS THE POINT: it closes/finalizes
the placement row BEFORE killing the session, so a dying agent's late
fail/deliver report resolves to a closed placement and can never re-route
consumers a second time. It is idempotent (no live placement → no-op) and
resolves a placement by unit slug and/or task id.
"""

from __future__ import annotations

import json

import pytest

pytestmark = [pytest.mark.agent, pytest.mark.smoke]

from awm.agents import placement
from awm.agents import agent_instances as ai
from awm.agents import orch_client
from awm.agents.dao import AgentsDAO
from awm.agents._time import now_ms


class FakeOrch:
    """Records B-ops so a late report reaching the orchestrator is observable."""

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


@pytest.fixture
def fake_orch(agents_env):
    impl = FakeOrch()
    orch_client.set_impl(impl)
    yield impl
    orch_client.reset_impl()


def _seed(agents_env, *, token="plt-1", task="T-1", scope="leaf-1", mode="worker"):
    """Insert an open placement row + lay out its workspace unit."""
    dao = AgentsDAO()
    iid = dao.open_task_instance(
        scope=scope, log_path=None, cli_session_id=None, started_at=now_ms(),
        mode=mode, task_ref=task, agent_ref="agt-1", placement_token=token)
    wp = agents_env["awm_dir"] / "services" / "workspace" / "units" / scope
    (wp / "deliverable").mkdir(parents=True, exist_ok=True)
    dao.merge_instance_data(iid, {
        "placement": {"mode": mode, "workspace_path": str(wp),
                      "contracts_in": [], "contracts_out": ["out:1"], "brief": ""},
        "staged": {}, "done": False, "attached": False,
    })
    return iid, str(wp)


def _data(iid):
    return json.loads(AgentsDAO().get_instance(iid)["data"])


_AS = "leaf-1"


class TestStopPlacement:
    async def test_closes_row_before_killing_session(
            self, agents_env, fake_orch, monkeypatch):
        iid, _ = _seed(agents_env)
        observed: dict = {}
        orig = ai.stop_session

        async def spy(instance_id):
            # Record whether the placement row is ALREADY closed at kill time.
            observed["closed_before_kill"] = bool(
                _data(iid).get("placement_outcome"))
            return await orig(instance_id)

        monkeypatch.setattr(ai, "stop_session", spy)
        res = await placement.stop_placement({"scope": "leaf-1", "task_id": "T-1"})
        assert res["ok"] is True and res["stopped"] is True
        assert observed["closed_before_kill"] is True
        assert _data(iid)["placement_outcome"] == "cancelled"

    async def test_retains_unit(self, agents_env, fake_orch):
        _seed(agents_env)
        await placement.stop_placement({"scope": "leaf-1"})
        names = [(c[0], c[1]) for c in agents_env["calls"]]
        assert ("workspace", "workspace_retain") in names

    async def test_idempotent_no_live_placement(self, agents_env, fake_orch):
        res = await placement.stop_placement({"scope": "nope", "task_id": "nope"})
        assert res["ok"] is True and res["stopped"] is False

    async def test_second_call_is_a_noop(self, agents_env, fake_orch):
        _seed(agents_env)
        first = await placement.stop_placement({"scope": "leaf-1"})
        second = await placement.stop_placement({"scope": "leaf-1"})
        assert first["stopped"] is True
        assert second["stopped"] is False       # already closed

    async def test_resolves_by_task_id_without_slug(self, agents_env, fake_orch):
        iid, _ = _seed(agents_env, scope="leaf-Z", task="T-Z")
        res = await placement.stop_placement({"task_id": "T-Z"})
        assert res["stopped"] is True and res["scope"] == "leaf-Z"
        assert _data(iid).get("placement_outcome") == "cancelled"

    async def test_late_report_after_stop_is_rejected(self, agents_env, fake_orch):
        _seed(agents_env)
        await placement.stop_placement({"scope": "leaf-1"})
        # A dying agent's late fail/deliver resolves to the CLOSED placement and
        # is rejected — it can never re-route consumers a second time.
        with pytest.raises(placement.PlacementError):
            await placement.relay_task_fail(
                {"reason_type": "transient-error"}, _AS)
        # ...and no fail B-op reached the orchestrator from that late report.
        assert not any(n == "fail" for n, _ in fake_orch.calls)


class TestManifestOmission:
    def test_stop_placement_handled_but_not_manifested(self):
        from awm.agents import hub_adapter
        # Reachable via the catch-all (in HANDLERS) but NOT an MCP tool.
        assert "stop_placement" in hub_adapter.HANDLERS
        tools = {f.get("tool") or f["name"]
                 for f in hub_adapter.API_MANIFEST["functions"]}
        assert "agent_stop_placement" not in tools
        assert "stop_placement" not in tools
