"""T3: worker-tool relays — token resolution, resolved refs, reclaim ordering."""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.agent, pytest.mark.smoke]

from awm.agents import placement
from awm.agents import orch_client
from awm.agents.dao import AgentsDAO
from awm.agents._time import now_ms


class FakeOrch:
    """Records B-op calls into a shared log so ordering vs scope_complete is
    observable; returns a recognizable ack."""

    def __init__(self, log):
        self.log = log

    async def claim(self, **kw):
        self.log.append(("orch", "claim", kw)); return {}

    async def deliver(self, **kw):
        self.log.append(("orch", "deliver", kw)); return {"ack": "deliver"}

    async def fail(self, **kw):
        self.log.append(("orch", "fail", kw)); return {"ack": "fail"}

    async def decompose_commit(self, **kw):
        self.log.append(("orch", "decompose_commit", kw)); return {"ack": "dc"}


@pytest.fixture
def fake_orch(agents_env):
    impl = FakeOrch(agents_env["calls"])
    orch_client.set_impl(impl)
    yield impl
    orch_client.reset_impl()


def _seed_placement(*, token="plt-1", task="T-1", agent="agt-1",
                    project="p", scope="leaf-1"):
    dao = AgentsDAO()
    return dao.open_task_instance(
        project=project, scope=scope, log_path=None, cli_session_id=None,
        started_at=now_ms(), mode="worker", task_ref=task, agent_ref=agent,
        placement_token=token)


class TestTokenResolution:
    async def test_unknown_token_rejected(self, agents_env, fake_orch):
        with pytest.raises(placement.PlacementError):
            await placement.relay_deliver({"placement_token": "nope"})

    async def test_missing_token_rejected(self, agents_env, fake_orch):
        with pytest.raises(placement.PlacementError):
            await placement.relay_deliver({})

    async def test_closed_placement_rejected(self, agents_env, fake_orch):
        iid = _seed_placement()
        AgentsDAO().close_placement(iid, outcome="delivered")
        with pytest.raises(placement.PlacementError):
            await placement.relay_deliver({"placement_token": "plt-1"})


class TestRelayUsesResolvedRefs:
    async def test_deliver_ignores_worker_supplied_refs(self, agents_env, fake_orch):
        _seed_placement(token="plt-d", task="T-real", agent="agt-real")
        await placement.relay_deliver({
            "placement_token": "plt-d", "contract": "c", "payload_ref": "art:1",
            "task_id": "SPOOF", "agent_ref": "SPOOF",
        })
        op, _name, kw = next(c for c in fake_orch.log if c[1] == "deliver")
        assert kw["task_id"] == "T-real"
        assert kw["agent_ref"] == "agt-real"
        assert kw["payload_ref"] == "art:1"


class TestReclaimOrdering:
    async def test_deliver_reclaims_after_ack(self, agents_env, fake_orch):
        iid = _seed_placement(token="plt-x")
        await placement.relay_deliver({"placement_token": "plt-x",
                                       "payload_ref": "art:1"})
        names = [(c[0], c[1]) for c in agents_env["calls"]]
        assert ("orch", "deliver") in names
        assert ("scopes", "scope_complete") in names
        assert names.index(("orch", "deliver")) < names.index(("scopes", "scope_complete"))
        assert AgentsDAO().get_instance(iid)["data"].find("delivered") != -1

    async def test_fail_reclaims_after_ack(self, agents_env, fake_orch):
        iid = _seed_placement(token="plt-f")
        await placement.relay_fail({"placement_token": "plt-f",
                                    "reason_type": "blocked",
                                    "reason_text": "x", "partial_ref": "art:p"})
        names = [(c[0], c[1]) for c in agents_env["calls"]]
        assert names.index(("orch", "fail")) < names.index(("scopes", "scope_complete"))
        op, _n, kw = next(c for c in fake_orch.log if c[1] == "fail")
        assert kw["partial_ref"] == "art:p"

    async def test_decompose_does_not_reclaim(self, agents_env, fake_orch):
        _seed_placement(token="plt-dc")
        await placement.relay_decompose({"placement_token": "plt-dc",
                                         "children": [{"id": "c1"}],
                                         "edges": [], "contracts": []})
        names = [(c[0], c[1]) for c in agents_env["calls"]]
        assert ("orch", "decompose_commit") in names
        assert ("scopes", "scope_complete") not in names  # scope retained
