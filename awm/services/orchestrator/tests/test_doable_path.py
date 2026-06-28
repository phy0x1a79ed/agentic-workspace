"""The doable path on the explicit state machine: a no-dependency task is
attached upstream of root, travels the worker legs (plan → verify → work),
delivers, completes — and root completes with it.

attach (born blocked, no deps -> ready -> planning)
  -> deliver("plan") -> plan_delivered -> verifying_plan
  -> approve_plan -> plan_approved -> active
  -> deliver(real contract) -> completed -> root completes too.
"""

from __future__ import annotations

import json

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.smoke]


def _attach(orch, *, goal="do it", produces=(("c1", "the deliverable"),)):
    """Attach a leaf task upstream of root, producing the named contract(s)."""
    return orch.operations.orch_task_attach({
        "goal": goal,
        "produces": [{"name": n, "spec": s} for (n, s) in produces],
    })


def test_task_is_dispatched_at_the_plan_leg(orch):
    res = _attach(orch)
    tid = res["task_id"]
    task = orch.DAO().get_task(tid)
    # blocked (no deps) -> ready -> planning: the first leg is a PLAN placement.
    assert task["state"] == "planning"
    assert task["mode"] == "plan"
    assert task["agent_ref"] == f"agent:{task['workspace_slug']}"
    assert task["placement_token"]
    # The task is wired upstream of root (root is its consumer).
    assert res["consumer"] == orch.DAO().get_root()["id"]
    # The plan leg stages the reserved "plan" deliverable: no real contracts_out
    # on the wire (the agents side defaults ["plan"]); the real produced-contract
    # names ride the brief so the plan agent knows its target.
    payload = orch.placements[(tid, "plan")]
    assert payload["mode"] == "plan"
    assert payload["contracts_out"] == []
    assert "c1" in json.loads(payload["brief"])["produces"]


def test_plan_then_verify_then_active(orch):
    """Each leg is a distinct placement; the verifier gets the plan + objective
    and no filesystem inputs."""
    res = _attach(orch)
    tid = res["task_id"]
    dao = orch.DAO()

    # Reserved plan handoff: planning -> plan_delivered, verifier dispatched.
    orch.operations.deliver({"task_id": tid, "contract": "plan",
                             "payload_ref": "artifact:plan"})
    t = dao.get_task(tid)
    assert t["state"] == "verifying_plan"
    assert t["plan_ref"] == "artifact:plan"
    vpayload = orch.placements[(tid, "verify")]
    brief = json.loads(vpayload["brief"])
    assert brief["mode"] == "verify"
    assert brief["plan_ref"] == "artifact:plan"
    assert vpayload["contracts_in"] == []  # fs-less verifier

    # Approve -> plan_approved -> worker placement (active).
    orch.operations.approve_plan({"task_id": tid})
    t = dao.get_task(tid)
    assert t["state"] == "active"
    assert t["mode"] == "worker"
    assert (tid, "worker") in orch.placements


def test_reject_plan_replans_and_spends_budget(orch):
    res = _attach(orch)
    tid = res["task_id"]
    dao = orch.DAO()
    budget0 = dao.get_task(tid)["replan_budget"]

    orch.operations.deliver({"task_id": tid, "contract": "plan",
                             "payload_ref": "artifact:plan1"})
    orch.operations.reject_plan({"task_id": tid,
                                 "reason_text": "spurious deliverable"})
    t = dao.get_task(tid)
    # Back at the plan leg (re-dispatched), one budget unit spent, plan discarded.
    assert t["state"] == "planning"
    assert t["replan_budget"] == budget0 - 1
    assert t["plan_ref"] is None


def test_deliver_completes_task_and_root(orch):
    res = _attach(orch)
    tid = res["task_id"]
    dao = orch.DAO()
    orch.advance_to_active(tid)
    task = dao.get_task(tid)

    reply = orch.operations.deliver({
        "task_id": tid, "agent_ref": task["agent_ref"],
        "contract": "c1", "payload_ref": "artifact:result",
    })
    assert reply["ok"] is True
    assert reply["state"] == "completed"

    fresh = dao.get_task(tid)
    assert fresh["state"] == "completed"
    assert fresh["agent_ref"] is None  # placement cleared on completion

    # Root consumed the task's contract; with its sole prerequisite delivered it
    # completes too (a sentinel: it never got a worker).
    root = dao.get_root()
    assert root["state"] == "completed"
    assert root["agent_ref"] is None

    status = orch.operations.orch_status({})  # global
    assert status["complete"] is True
    assert status["counts"] == {"completed": 2}  # task + root
    assert status["escalations"] == []

    # The plan handoff + the real delivery are both recorded in append-only
    # memory; the real artifact ref is the last entry.
    mems = dao.list_attempt_memories(tid)
    assert [m["outcome"] for m in mems] == ["delivered", "delivered"]
    assert mems[-1]["payload_ref"] == "artifact:result"


def test_empty_payload_fails_screen(orch):
    res = _attach(orch)
    tid = res["task_id"]
    orch.advance_to_active(tid)
    task = orch.DAO().get_task(tid)
    reply = orch.operations.deliver({
        "task_id": tid, "agent_ref": task["agent_ref"],
        "contract": "c1", "payload_ref": "   ",
    })
    assert reply["ok"] is False
    assert orch.DAO().get_task(tid)["state"] == "active"  # unchanged


def test_wrong_agent_ref_is_rejected(orch):
    res = _attach(orch)
    tid = res["task_id"]
    orch.advance_to_active(tid)
    with pytest.raises(ValueError):
        orch.operations.deliver({
            "task_id": tid, "agent_ref": "agent:imposter",
            "contract": "c1", "payload_ref": "artifact:x",
        })


def test_transient_failure_auto_retries_same_leg(orch):
    res = _attach(orch)
    tid = res["task_id"]
    dao = orch.DAO()
    orch.advance_to_active(tid)
    task = dao.get_task(tid)
    orch.operations.fail({
        "task_id": tid, "agent_ref": task["agent_ref"],
        "reason_type": "transient-error", "reason_text": "agent died",
    })
    # active transient -> plan_approved (retry the worker) -> re-dispatched ->
    # active again, retry_count bumped.
    fresh = dao.get_task(tid)
    assert fresh["state"] == "active"
    assert fresh["retry_count"] == 1
