"""The decomposition path: fail-as-signal -> review -> children -> completion.

plan_create -> fail(needs-decomposition) -> failed -> planner dispatched
-> analyzing -> decompose_commit (A, B; B depends on A) -> A ready/dispatched,
B pending -> deliver A -> B ready/dispatched -> deliver B -> aggregate -> root
delivered -> plan complete.
"""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.smoke]


def _decompose(orch, root, planner_ref):
    return orch.operations.decompose_commit({
        "task_id": root, "agent_ref": planner_ref,
        "children": [{"ref": "A", "goal": "part A"},
                     {"ref": "B", "goal": "part B"}],
        "contracts": [{"name": "cA", "spec": "A out", "producer": "A"},
                      {"name": "cB", "spec": "B out", "producer": "B"}],
        "edges": [{"consumer": "B", "contract": "cA"}],
    })


def test_fail_routes_to_review_and_dispatches_planner(orch):
    res = orch.operations.orch_plan_create({"project": "p", "goal": "big job"})
    root = res["task_id"]
    dao = orch.DAO()
    worker_ref = dao.get_task(root)["agent_ref"]

    orch.operations.fail({
        "task_id": root, "agent_ref": worker_ref,
        "reason_type": "needs-decomposition", "reason_text": "too big",
    })
    # failed -> analyzing (planner placement dispatched).
    task = dao.get_task(root)
    assert task["state"] == "analyzing"
    assert task["mode"] == "planner"
    assert task["agent_ref"] == f"agent:{task['scope_slug']}"
    # The planner reuses the worker's freed scope slug to see partial work.
    assert task["scope_slug"] == f"orch-{root[:8]}"
    # The planner brief carries the review reason.
    payload = orch.placements[root]
    assert payload["mode"] == "planner"


def test_decompose_creates_children_with_dependency(orch):
    res = orch.operations.orch_plan_create({"project": "p", "goal": "big job"})
    root = res["task_id"]
    dao = orch.DAO()
    orch.operations.fail({
        "task_id": root, "agent_ref": dao.get_task(root)["agent_ref"],
        "reason_type": "needs-decomposition", "reason_text": "too big",
    })
    planner_ref = dao.get_task(root)["agent_ref"]

    dc = _decompose(orch, root, planner_ref)
    assert dc["ok"] is True

    root_task = dao.get_task(root)
    assert root_task["state"] == "pending"  # composite awaiting children
    assert root_task["replan_budget"] == 1  # one unit spent (default 2)

    by_goal = {dao.get_task(c)["goal"]: dao.get_task(c) for c in dc["children"]}
    a, b = by_goal["part A"], by_goal["part B"]
    assert a["parent_id"] == root and b["parent_id"] == root
    # A has no deps -> ready -> dispatched -> active; B waits on cA -> pending.
    assert a["state"] == "active"
    assert b["state"] == "pending"


def test_full_decomposition_runs_to_completion(orch):
    res = orch.operations.orch_plan_create({"project": "p", "goal": "big job"})
    root = res["task_id"]
    dao = orch.DAO()
    orch.operations.fail({
        "task_id": root, "agent_ref": dao.get_task(root)["agent_ref"],
        "reason_type": "needs-decomposition", "reason_text": "too big",
    })
    dc = _decompose(orch, root, dao.get_task(root)["agent_ref"])
    by_goal = {dao.get_task(c)["goal"]: dao.get_task(c) for c in dc["children"]}
    a, b = by_goal["part A"], by_goal["part B"]

    # Deliver A's contract -> B becomes ready -> dispatched -> active.
    orch.operations.deliver({
        "task_id": a["id"], "agent_ref": a["agent_ref"],
        "contract": "cA", "payload_ref": "artifact:A",
    })
    assert dao.get_task(a["id"])["state"] == "delivered"
    b_now = dao.get_task(b["id"])
    assert b_now["state"] == "active"

    # Deliver B -> both children delivered -> aggregate -> root delivered.
    orch.operations.deliver({
        "task_id": b["id"], "agent_ref": b_now["agent_ref"],
        "contract": "cB", "payload_ref": "artifact:B",
    })
    assert dao.get_task(b["id"])["state"] == "delivered"
    assert dao.get_task(root)["state"] == "delivered"

    status = orch.operations.orch_status({"project": "p"})
    assert status["complete"] is True
    assert status["counts"] == {"delivered": 3}


def test_decompose_rejects_cyclic_edges(orch):
    res = orch.operations.orch_plan_create({"project": "p", "goal": "big job"})
    root = res["task_id"]
    dao = orch.DAO()
    orch.operations.fail({
        "task_id": root, "agent_ref": dao.get_task(root)["agent_ref"],
        "reason_type": "needs-decomposition", "reason_text": "too big",
    })
    planner_ref = dao.get_task(root)["agent_ref"]

    with pytest.raises(ValueError, match="cycle"):
        orch.operations.decompose_commit({
            "task_id": root, "agent_ref": planner_ref,
            "children": [{"ref": "A", "goal": "a"}, {"ref": "B", "goal": "b"}],
            "contracts": [{"name": "cA", "spec": "", "producer": "A"},
                          {"name": "cB", "spec": "", "producer": "B"}],
            # A depends on cB and B depends on cA -> a cycle.
            "edges": [{"consumer": "A", "contract": "cB"},
                      {"consumer": "B", "contract": "cA"}],
        })


def test_planner_giveup_discards_and_escalates(orch):
    """A planner that exhausts the replan budget discards the node; a root
    discard surfaces as an escalation."""
    res = orch.operations.orch_plan_create({"project": "p", "goal": "big job"})
    root = res["task_id"]
    dao = orch.DAO()
    # Drive the node to analyzing, then have the planner give up repeatedly
    # until replan_budget (2) is spent.
    orch.operations.fail({
        "task_id": root, "agent_ref": dao.get_task(root)["agent_ref"],
        "reason_type": "needs-decomposition", "reason_text": "too big",
    })
    # First planner give-up: budget 2 -> 1, back to failed -> re-dispatched.
    orch.operations.fail({
        "task_id": root, "agent_ref": dao.get_task(root)["agent_ref"],
        "reason_type": "needs-decomposition", "reason_text": "cant",
    })
    assert dao.get_task(root)["state"] == "analyzing"
    # Second give-up: budget 1 -> 0 -> discarded.
    orch.operations.fail({
        "task_id": root, "agent_ref": dao.get_task(root)["agent_ref"],
        "reason_type": "needs-decomposition", "reason_text": "cant",
    })
    assert dao.get_task(root)["state"] == "discarded"

    status = orch.operations.orch_status({"project": "p"})
    assert any(e["task_id"] == root for e in status["escalations"])
