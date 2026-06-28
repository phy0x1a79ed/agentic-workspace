"""The creation-placement (SPECIFYING), the attached flag, and plan-rejection
budget exhaustion.

orch_task_create births a vague task straight into a human-attended planner
placement (decompose_pending -> decomposing) without spending replan budget; the
planner's brief carries reason="initial". An empty spec commit drops the task
through the normal worker legs. set_attached toggles the orthogonal flag.
reject_plan with no budget left abandons the task.
"""

from __future__ import annotations

import json

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.smoke]


def _create(orch, goal="figure out what to build"):
    return orch.operations.orch_task_create({"goal": goal})


def test_create_places_attended_initial_planner_without_spending_budget(orch):
    res = _create(orch)
    tid = res["task_id"]
    task = orch.DAO().get_task(tid)
    # Born into the planner placement, human-attended, full budget intact.
    assert task["state"] == "decomposing"
    assert task["mode"] == "planner"
    assert bool(task["attached"]) is True
    assert task["replan_budget"] == 2  # initial specification is free
    # The planner brief flags this as the initial specification, not a re-plan.
    payload = orch.placements[(tid, "planner")]
    brief = json.loads(payload["brief"])
    assert brief["reason"] == "initial"
    assert "review_reason" not in brief
    # It hangs in the DAG: root depends on its synthetic deliverable.
    root = orch.DAO().get_root()
    assert res["consumer"] == root["id"]


def test_empty_spec_commit_flows_through_the_worker_legs(orch):
    res = _create(orch)
    tid = res["task_id"]
    dao = orch.DAO()
    planner_ref = dao.get_task(tid)["agent_ref"]

    # A trivial specification: no children, no new contracts. The task has no new
    # dependency, so it restarts at the PLAN leg.
    dc = orch.operations.decompose_commit({
        "task_id": tid, "agent_ref": planner_ref,
        "children": [], "contracts": [], "edges": [],
    })
    assert dc["ok"] is True
    assert dao.get_task(tid)["state"] == "planning"

    # And it can run to completion through the legs + its synthetic deliverable.
    orch.advance_to_active(tid)
    task = dao.get_task(tid)
    deliverable = dao.list_contracts_by_producer(tid)[0]["name"]
    orch.operations.deliver({
        "task_id": tid, "agent_ref": task["agent_ref"],
        "contract": deliverable, "payload_ref": "artifact:spec-result",
    })
    assert dao.get_task(tid)["state"] == "completed"
    assert dao.get_root()["state"] == "completed"


def test_set_attached_toggles_the_flag(orch):
    res = orch.operations.orch_task_attach({
        "goal": "do it",
        "produces": [{"name": "c1", "spec": "out"}]})
    tid = res["task_id"]
    dao = orch.DAO()
    agent_ref = dao.get_task(tid)["agent_ref"]

    assert bool(dao.get_task(tid)["attached"]) is False
    reply = orch.operations.set_attached(
        {"task_id": tid, "agent_ref": agent_ref, "attached": True})
    assert reply["ok"] is True and reply["attached"] is True
    assert bool(dao.get_task(tid)["attached"]) is True

    orch.operations.set_attached(
        {"task_id": tid, "agent_ref": agent_ref, "attached": False})
    assert bool(dao.get_task(tid)["attached"]) is False


def test_reject_plan_exhausting_budget_abandons_and_escalates(orch):
    res = orch.operations.orch_task_attach({
        "goal": "do it",
        "produces": [{"name": "c1", "spec": "out"}]})
    tid = res["task_id"]
    dao = orch.DAO()

    # Re-plan until budget (default 2) is spent: each rejection costs one unit.
    for _ in range(2):
        orch.operations.deliver({"task_id": tid, "contract": "plan",
                                 "payload_ref": "artifact:plan"})
        orch.operations.reject_plan({"task_id": tid, "reason_text": "no good"})
    assert dao.get_task(tid)["replan_budget"] == 0
    assert dao.get_task(tid)["state"] == "planning"  # last re-plan re-dispatched

    # One more rejection with no budget left -> abandoned, root escalates.
    orch.operations.deliver({"task_id": tid, "contract": "plan",
                             "payload_ref": "artifact:plan"})
    orch.operations.reject_plan({"task_id": tid, "reason_text": "still no good"})
    assert dao.get_task(tid)["state"] == "abandoned"
    assert dao.get_root()["state"] == "abandoned"
    status = orch.operations.orch_status({})
    assert any(e["task_id"] == dao.get_root()["id"]
               for e in status["escalations"])
