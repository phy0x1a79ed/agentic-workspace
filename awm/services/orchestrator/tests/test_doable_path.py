"""The doable path: a no-dependency task is attached upstream of root,
dispatched, delivered, completed — and root completes with it.

attach (task born blocked, no deps -> ready -> dispatched -> active)
-> deliver -> completed -> root (its sole prerequisite) completes too.
"""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.smoke]


def _attach(orch, *, goal="do it", produces=(("c1", "the deliverable"),)):
    """Attach a leaf task upstream of root, producing the named contract(s)."""
    return orch.operations.orch_task_attach({
        "project": "p", "goal": goal,
        "produces": [{"name": n, "spec": s} for (n, s) in produces],
    })


def test_task_is_dispatched_at_attach(orch):
    res = _attach(orch)
    tid = res["task_id"]
    task = orch.DAO().get_task(tid)
    # blocked (no deps) -> ready -> active flip at dispatch, recording the stub.
    assert task["state"] == "active"
    assert task["mode"] == "worker"
    assert task["agent_ref"] == f"agent:{task['scope_slug']}"
    assert task["placement_token"]
    # The task is wired upstream of root (root is its consumer).
    assert res["consumer"] == orch.DAO().get_root()["id"]
    # The placement payload carried the outgoing contract.
    payload = orch.placements[tid]
    assert payload["mode"] == "worker"
    assert any(c["name"] == "c1" for c in payload["contracts_out"])


def test_deliver_completes_task_and_root(orch):
    res = _attach(orch)
    tid = res["task_id"]
    dao = orch.DAO()
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

    # The delivery + its artifact ref are recorded in append-only memory.
    mems = dao.list_attempt_memories(tid)
    assert [m["outcome"] for m in mems] == ["delivered"]
    assert mems[0]["payload_ref"] == "artifact:result"


def test_empty_payload_fails_screen(orch):
    res = _attach(orch)
    tid = res["task_id"]
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
    with pytest.raises(ValueError):
        orch.operations.deliver({
            "task_id": tid, "agent_ref": "agent:imposter",
            "contract": "c1", "payload_ref": "artifact:x",
        })


def test_transient_failure_auto_retries_to_ready_then_redispatched(orch):
    res = _attach(orch)
    tid = res["task_id"]
    dao = orch.DAO()
    task = dao.get_task(tid)
    orch.operations.fail({
        "task_id": tid, "agent_ref": task["agent_ref"],
        "reason_type": "transient-error", "reason_text": "agent died",
    })
    # transient -> ready (retry_count bumped) -> re-dispatched -> active again.
    fresh = dao.get_task(tid)
    assert fresh["state"] == "active"
    assert fresh["retry_count"] == 1
