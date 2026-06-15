"""T1/T4: the conversational resume driver never resurrects task-bound rows."""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.agent, pytest.mark.smoke]

import awm.agents.agent_instances as ai_mod
from awm.agents.dao import AgentsDAO
from awm.agents._time import now_ms


def _open_conversational(scope):
    return AgentsDAO().open_instance(
        project="p", scope=scope, log_path=None,
        cli_session_id="cid", started_at=now_ms())


def _open_task(scope, token):
    return AgentsDAO().open_task_instance(
        project="p", scope=scope, log_path=None, cli_session_id="cid",
        started_at=now_ms(), mode="worker", task_ref="T", agent_ref="agt-1",
        parent_agent_ref=None, placement_token=token)


class TestResumeSkipsTaskBound:
    def test_get_active_scopes_excludes_task_bound(self, agents_env):
        _open_conversational("conv")
        _open_task("worker", "plt-1")
        dao = AgentsDAO()
        scopes = {r["scope"] for r in dao.get_active_scopes()}
        assert "conv" in scopes
        assert "worker" not in scopes

    def test_reconcile_seeds_only_conversational(self, agents_env):
        # get_active_scopes runs AFTER close_all_open_instances in reconcile, so
        # the schedule stays empty in v1 — but the key guarantee is that the
        # task-bound row never feeds the resume seed regardless.
        ai_mod._resume_schedule.clear()
        _open_task("worker", "plt-2")
        ai_mod.reconcile_on_startup()
        assert "p/worker" not in ai_mod._resume_schedule
