"""The decomposition path: a too-big task self-decomposes into an upstream
sub-DAG, depends on it, re-runs once it delivers — and a planner give-up
abandons the node and routes its consumer (root) to escalation.

attach (task active) -> fail(needs-decomposition) -> decomposing (planner)
-> decompose_commit (A, B; B depends on A; task depends on terminal cB) -> task
blocked, A active, B blocked -> deliver A -> B active -> deliver B -> task
ready/active again -> deliver task's own contract -> completed -> root completes.
"""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.smoke]


def _attach(orch, goal="big job"):
    """Attach a leaf task (producing c1) upstream of root."""
    return orch.operations.orch_task_attach({
        "project": "p", "goal": goal,
        "produces": [{"name": "c1", "spec": "the deliverable"}],
    })


def _to_decomposing(orch, tid):
    """Drive an active task into decomposing (planner placed)."""
    dao = orch.DAO()
    orch.operations.fail({
        "task_id": tid, "agent_ref": dao.get_task(tid)["agent_ref"],
        "reason_type": "needs-decomposition", "reason_text": "too big",
    })


def _decompose(orch, tid, planner_ref):
    """Commit a 2-node sub-DAG (B depends on A); cB is the terminal sink, so the
    decomposing task auto-depends on it."""
    return orch.operations.decompose_commit({
        "task_id": tid, "agent_ref": planner_ref,
        "children": [{"ref": "A", "goal": "part A"},
                     {"ref": "B", "goal": "part B"}],
        "contracts": [{"name": "cA", "spec": "A out", "producer": "A"},
                      {"name": "cB", "spec": "B out", "producer": "B"}],
        "edges": [{"consumer": "B", "contract": "cA"}],
    })


def test_needs_decomposition_routes_to_decomposing_and_dispatches_planner(orch):
    res = _attach(orch)
    tid = res["task_id"]
    dao = orch.DAO()
    worker_ref = dao.get_task(tid)["agent_ref"]

    orch.operations.fail({
        "task_id": tid, "agent_ref": worker_ref,
        "reason_type": "needs-decomposition", "reason_text": "too big",
    })
    # active -> decomposing (planner placement dispatched; agent_ref re-recorded).
    task = dao.get_task(tid)
    assert task["state"] == "decomposing"
    assert task["mode"] == "planner"
    assert task["agent_ref"] == f"agent:{task['scope_slug']}"
    # The planner reuses the worker's freed scope slug to see partial work.
    assert task["scope_slug"] == f"orch-{tid[:8]}"
    payload = orch.placements[tid]
    assert payload["mode"] == "planner"


def test_decompose_creates_subdag_and_task_depends_on_it(orch):
    res = _attach(orch)
    tid = res["task_id"]
    dao = orch.DAO()
    _to_decomposing(orch, tid)
    planner_ref = dao.get_task(tid)["agent_ref"]

    dc = _decompose(orch, tid, planner_ref)
    assert dc["ok"] is True

    task = dao.get_task(tid)
    assert task["state"] == "blocked"      # awaiting its own sub-DAG
    assert task["replan_budget"] == 1      # one unit spent (default 2)

    by_goal = {dao.get_task(c)["goal"]: dao.get_task(c) for c in dc["children"]}
    a, b = by_goal["part A"], by_goal["part B"]
    # A has no deps -> ready -> dispatched -> active; B waits on cA -> blocked.
    assert a["state"] == "active"
    assert b["state"] == "blocked"

    # The decomposing task auto-depends on cB (the sub-DAG's terminal sink).
    incoming = {e["name"] for e in dao.list_incoming_edges(tid)}
    assert incoming == {"cB"}


def test_full_decomposition_runs_to_completion(orch):
    res = _attach(orch)
    tid = res["task_id"]
    dao = orch.DAO()
    _to_decomposing(orch, tid)
    dc = _decompose(orch, tid, dao.get_task(tid)["agent_ref"])
    by_goal = {dao.get_task(c)["goal"]: dao.get_task(c) for c in dc["children"]}
    a, b = by_goal["part A"], by_goal["part B"]

    # Deliver A's contract -> B becomes ready -> dispatched -> active.
    orch.operations.deliver({
        "task_id": a["id"], "agent_ref": a["agent_ref"],
        "contract": "cA", "payload_ref": "artifact:A",
    })
    assert dao.get_task(a["id"])["state"] == "completed"
    b_now = dao.get_task(b["id"])
    assert b_now["state"] == "active"

    # Deliver B's terminal contract -> the decomposing task's dependency is met,
    # so it re-runs as a normal worker (blocked -> ready -> active).
    orch.operations.deliver({
        "task_id": b["id"], "agent_ref": b_now["agent_ref"],
        "contract": "cB", "payload_ref": "artifact:B",
    })
    assert dao.get_task(b["id"])["state"] == "completed"
    task_now = dao.get_task(tid)
    assert task_now["state"] == "active"   # re-dispatched, NOT auto-completed

    # The task finally delivers its OWN contract -> completed -> root completes.
    orch.operations.deliver({
        "task_id": tid, "agent_ref": task_now["agent_ref"],
        "contract": "c1", "payload_ref": "artifact:final",
    })
    assert dao.get_task(tid)["state"] == "completed"
    assert dao.get_root()["state"] == "completed"

    status = orch.operations.orch_status({})  # global
    assert status["complete"] is True
    assert status["counts"] == {"completed": 4}  # task + A + B + root


def test_decompose_rejects_cyclic_edges(orch):
    res = _attach(orch)
    tid = res["task_id"]
    dao = orch.DAO()
    _to_decomposing(orch, tid)
    planner_ref = dao.get_task(tid)["agent_ref"]

    with pytest.raises(ValueError, match="cycle"):
        orch.operations.decompose_commit({
            "task_id": tid, "agent_ref": planner_ref,
            "children": [{"ref": "A", "goal": "a"}, {"ref": "B", "goal": "b"}],
            "contracts": [{"name": "cA", "spec": "", "producer": "A"},
                          {"name": "cB", "spec": "", "producer": "B"}],
            # A depends on cB and B depends on cA -> a cycle.
            "edges": [{"consumer": "A", "contract": "cB"},
                      {"consumer": "B", "contract": "cA"}],
        })


def test_planner_giveup_abandons_and_escalates_via_root(orch):
    """A planner that exhausts the replan budget abandons the node and routes its
    consumer; the consumer here is root, which cannot re-plan, so it abandons and
    surfaces as an escalation."""
    res = _attach(orch)
    tid = res["task_id"]
    dao = orch.DAO()
    _to_decomposing(orch, tid)  # active -> decomposing (budget still 2)

    # First planner give-up: budget 2 -> 1, stays decomposing, re-dispatched.
    orch.operations.fail({
        "task_id": tid, "agent_ref": dao.get_task(tid)["agent_ref"],
        "reason_type": "needs-decomposition", "reason_text": "cant",
    })
    assert dao.get_task(tid)["state"] == "decomposing"
    assert dao.get_task(tid)["replan_budget"] == 1

    # Second give-up: budget 1 -> 0 -> abandoned; consumer (root) routed.
    orch.operations.fail({
        "task_id": tid, "agent_ref": dao.get_task(tid)["agent_ref"],
        "reason_type": "needs-decomposition", "reason_text": "cant",
    })
    assert dao.get_task(tid)["state"] == "abandoned"

    root = dao.get_root()
    assert root["state"] == "abandoned"  # could not re-plan -> escalation

    status = orch.operations.orch_status({})
    assert any(e["task_id"] == root["id"] for e in status["escalations"])
    assert status["complete"] is False
