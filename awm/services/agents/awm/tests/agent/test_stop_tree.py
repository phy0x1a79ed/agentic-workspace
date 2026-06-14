"""T5: stop_tree — single-agent stub (no lineage cascade yet)."""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.agent, pytest.mark.smoke]

from awm.agents import placement


class TestStopTreeStub:
    async def test_missing_agent_ref_rejected(self, agents_env):
        with pytest.raises(placement.PlacementError):
            await placement.stop_tree({})

    async def test_stops_matching_agent_no_cascade(self, agents_env, stub_core):
        res = await placement.place_on_task({
            "task_id": "T-1", "project": "p", "scope_slug": "leaf-1",
            "brief": "b", "mode": "worker"})
        out = await placement.stop_tree({"agent_ref": res["agent_ref"]})
        assert out["cascade"] is False
        assert len(out["stopped"]) == 1

    async def test_unknown_agent_ref_stops_nothing(self, agents_env, stub_core):
        out = await placement.stop_tree({"agent_ref": "agt-missing"})
        assert out["stopped"] == []
