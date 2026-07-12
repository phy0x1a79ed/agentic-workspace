"""Node lifecycle controls (T4) — orchestrator side.

The three public verbs a focus panel drives:

* ``orch_cancel`` — reject root/terminal; otherwise reuse the ONE give-up path
  (``kernel.abandon``) so consumers re-plan, best-effort stop the live
  placement (via the injectable stop seam), and clear the attach/steering state
  of a cancelled attended node; a queued-but-unplaced dispatch intent for a
  cancelled task self-cancels at the drain's rest-state re-check.
* ``orch_retry`` — accept ONLY failed/abandoned; reset to ready with a fresh
  budget, clear paused + stale steering bits (keep the objective + retained
  unit), record a human-retry memory, re-dispatch.
* ``orch_decompose`` — accept any resting non-terminal node; charge the
  decompose budget like the organic path and place a planner; reject
  root/out-states/terminals.
"""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.smoke]


def _attach(orch, *, goal="do it", consumer=None, produces=(("c1", ""),),
            depends_on=()):
    return orch.operations.orch_task_attach({
        "goal": goal,
        "consumer": consumer,
        "produces": [{"name": n, "spec": s} for (n, s) in produces],
        "depends_on": list(depends_on),
    })


# ---------------------------------------------------------------------------
# orch_cancel
# ---------------------------------------------------------------------------


class TestCancel:
    def test_rejects_root(self, orch):
        root = orch.DAO().ensure_root()
        out = orch.operations.orch_cancel({"task_id": root["id"]})
        assert out["ok"] is False

    def test_rejects_terminal(self, orch):
        tid = _attach(orch)["task_id"]
        orch.advance_to_active(tid)
        task = orch.DAO().get_task(tid)
        orch.operations.deliver({"task_id": tid, "agent_ref": task["agent_ref"],
                                 "contract": "c1", "payload_ref": "art:ok"})
        assert orch.DAO().get_task(tid)["state"] == "completed"
        assert orch.operations.orch_cancel({"task_id": tid})["ok"] is False

    def test_unknown_task_soft_error(self, orch):
        assert orch.operations.orch_cancel({"task_id": "nope"})["ok"] is False

    def test_abandons_via_the_giveup_path(self, orch):
        tid = _attach(orch)["task_id"]  # planning (a live placement)
        out = orch.operations.orch_cancel({"task_id": tid})
        assert out["ok"] is True and out["state"] == "abandoned"
        assert orch.DAO().get_task(tid)["state"] == "abandoned"

    def test_routes_consumers_to_replan_once(self, orch):
        # C consumes P's contract; cancelling P routes C to re-plan exactly once.
        cid = _attach(orch, goal="consumer", produces=(("final", ""),))["task_id"]
        pid = _attach(orch, goal="producer", consumer=cid,
                      produces=(("cP", ""),))["task_id"]
        cbudget0 = orch.DAO().get_task(cid)["replan_budget"]
        out = orch.operations.orch_cancel({"task_id": pid})
        assert out["ok"] is True
        assert orch.DAO().get_task(pid)["state"] == "abandoned"
        ct = orch.DAO().get_task(cid)
        # Sync dispatch places the planner inline: decompose_pending -> decomposing.
        assert ct["state"] in ("decompose_pending", "decomposing")
        assert ct["replan_budget"] == cbudget0 - 1

    def test_stops_live_placement_best_effort(self, orch, monkeypatch):
        calls: list = []
        monkeypatch.setattr(orch.operations, "_stop_placement_fn",
                            lambda slug, tid: calls.append((slug, tid)))
        tid = _attach(orch)["task_id"]  # planning; workspace_slug minted
        slug = orch.DAO().get_task(tid)["workspace_slug"]
        assert slug
        out = orch.operations.orch_cancel({"task_id": tid})
        assert out["ok"] is True
        assert calls == [(slug, tid)]

    def test_stop_failure_does_not_fail_cancel(self, orch, monkeypatch):
        def boom(slug, tid):
            raise RuntimeError("agents unreachable")
        monkeypatch.setattr(orch.operations, "_stop_placement_fn", boom)
        tid = _attach(orch)["task_id"]
        out = orch.operations.orch_cancel({"task_id": tid})
        assert out["ok"] is True
        assert orch.DAO().get_task(tid)["state"] == "abandoned"

    def test_queued_intent_self_cancels_at_drain(self, orch):
        # A task resting in ready with a not-yet-drained plan intent; cancelling
        # it first means the drain's rest-state re-check (dispatch._prepare) skips
        # the stale intent — no placement is built.
        dao = orch.DAO()
        tid = dao.create_task("queued", state="ready")
        orch.operations.orch_cancel({"task_id": tid})
        assert dao.get_task(tid)["state"] == "abandoned"
        assert orch.dispatch._prepare(tid, "plan") is None

    def test_attached_cancel_clears_attach_and_steering(self, orch):
        res = orch.operations.orch_node_open({"goal": "x"})  # born attached
        tid = res["task_id"]
        orch.operations.set_steering({
            "task_id": tid, "objective": "the record", "steer_agent_ready": True,
            "steer_user_done": True, "steer_requested": True})
        out = orch.operations.orch_cancel({"task_id": tid})
        assert out["ok"] is True
        t = orch.DAO().get_task(tid)
        assert bool(t["attached"]) is False
        assert (t["steer_user_done"], t["steer_agent_ready"],
                t["steer_requested"]) == (0, 0, 0)
        assert t["objective"] == "the record"   # the record is not erased


# ---------------------------------------------------------------------------
# orch_retry
# ---------------------------------------------------------------------------


class TestRetry:
    def test_rejects_non_terminal(self, orch):
        tid = _attach(orch)["task_id"]  # planning
        assert orch.operations.orch_retry({"task_id": tid})["ok"] is False

    def test_rejects_root(self, orch):
        dao = orch.DAO()
        root = dao.ensure_root()
        dao.update_task(root["id"], state="abandoned")
        assert orch.operations.orch_retry({"task_id": root["id"]})["ok"] is False

    def test_unknown_task_soft_error(self, orch):
        assert orch.operations.orch_retry({"task_id": "nope"})["ok"] is False

    def test_accepts_failed_and_regrants_fresh_budget(self, orch):
        dao = orch.DAO()
        tid = dao.create_task("dead", state="failed", replan_budget=0)
        dao.update_task(tid, retry_count=3)
        out = orch.operations.orch_retry({"task_id": tid})
        assert out["ok"] is True
        t = dao.get_task(tid)
        # Re-enters the legs at plan (sync dispatch flips ready -> planning).
        assert t["state"] in ("ready", "planning")
        assert t["replan_budget"] == orch.operations.FRESH_REPLAN_BUDGET
        assert t["retry_count"] == 0

    def test_clears_pause_and_steering_keeps_objective(self, orch):
        dao = orch.DAO()
        tid = dao.create_task("dead", state="abandoned")
        dao.update_task(tid, paused=1, attached=1, steer_user_done=1,
                        steer_agent_ready=1, steer_requested=1,
                        objective="keep me")
        orch.operations.orch_retry({"task_id": tid})
        t = dao.get_task(tid)
        assert t["paused"] == 0
        assert t["attached"] == 0                # retried nodes start detached
        assert (t["steer_user_done"], t["steer_agent_ready"],
                t["steer_requested"]) == (0, 0, 0)
        assert t["objective"] == "keep me"       # ratified intent is kept

    def test_keeps_retained_workspace_unit(self, orch):
        dao = orch.DAO()
        tid = dao.create_task("dead", state="failed")
        dao.update_task(tid, workspace_slug="orch-keep")
        orch.operations.orch_retry({"task_id": tid})
        assert dao.get_task(tid)["workspace_slug"] == "orch-keep"

    def test_records_memory_and_redispatches(self, orch):
        dao = orch.DAO()
        tid = dao.create_task("dead", state="abandoned")
        orch.operations.orch_retry({"task_id": tid})
        mems = dao.list_attempt_memories(tid)
        assert any(m["reason_type"] == "human-retry" for m in mems)
        assert (tid, "plan") in orch.placements


# ---------------------------------------------------------------------------
# orch_decompose
# ---------------------------------------------------------------------------


class TestDecompose:
    def _blocked_consumer(self, orch):
        """A node resting in ``blocked`` (waiting on an undelivered contract) —
        the only stable resting state under synchronous dispatch (ready /
        plan_approved / … place inline and become out-states)."""
        _attach(orch, goal="producer", produces=(("cP", ""),))  # planning
        cid = _attach(orch, goal="consumer", depends_on=("cP",),
                      produces=(("final", ""),))["task_id"]
        assert orch.DAO().get_task(cid)["state"] == "blocked"
        return cid

    def test_rejects_root(self, orch):
        root = orch.DAO().ensure_root()
        assert orch.operations.orch_decompose({"task_id": root["id"]})["ok"] is False

    def test_rejects_out_state(self, orch):
        tid = _attach(orch)["task_id"]  # planning (a live placement)
        assert orch.operations.orch_decompose({"task_id": tid})["ok"] is False

    def test_rejects_terminal(self, orch):
        dao = orch.DAO()
        tid = dao.create_task("done", state="completed")
        assert orch.operations.orch_decompose({"task_id": tid})["ok"] is False

    def test_unknown_task_soft_error(self, orch):
        assert orch.operations.orch_decompose({"task_id": "nope"})["ok"] is False

    def test_resting_node_charges_budget_and_places_planner(self, orch):
        cid = self._blocked_consumer(orch)
        budget0 = orch.DAO().get_task(cid)["replan_budget"]
        out = orch.operations.orch_decompose({"task_id": cid})
        assert out["ok"] is True
        t = orch.DAO().get_task(cid)
        # decompose_pending -> (sync dispatch) decomposing; one budget unit spent.
        assert t["state"] == "decomposing"
        assert t["replan_budget"] == budget0 - 1
        assert (cid, "planner") in orch.placements

    def test_out_of_budget_abandons(self, orch):
        cid = self._blocked_consumer(orch)
        orch.DAO().update_task(cid, replan_budget=0)
        out = orch.operations.orch_decompose({"task_id": cid})
        assert out["ok"] is True
        # No budget to decompose -> the give-up path abandons it.
        assert orch.DAO().get_task(cid)["state"] == "abandoned"
