"""The doable path: a no-dependency task is dispatched, delivered, completed.

plan_create -> (root born ready, dispatched -> active) -> deliver -> delivered
-> plan complete.
"""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.smoke]


def test_root_is_dispatched_at_creation(orch):
    res = orch.operations.orch_plan_create({"project": "p", "goal": "do it"})
    root = res["task_id"]
    task = orch.DAO().get_task(root)
    # ready -> active flip happened at dispatch, recording the stub placement.
    assert task["state"] == "active"
    assert task["mode"] == "worker"
    assert task["agent_ref"] == f"agent:{task['scope_slug']}"
    assert task["placement_token"]
    # The placement payload carried the outgoing contract.
    payload = orch.placements[root]
    assert payload["mode"] == "worker"
    assert any(c["name"] == res["contract"] for c in payload["contracts_out"])


def test_deliver_completes_the_plan(orch):
    res = orch.operations.orch_plan_create({"project": "p", "goal": "do it"})
    root = res["task_id"]
    dao = orch.DAO()
    task = dao.get_task(root)

    reply = orch.operations.deliver({
        "task_id": root, "agent_ref": task["agent_ref"],
        "contract": res["contract"], "payload_ref": "artifact:result",
    })
    assert reply["ok"] is True
    assert reply["state"] == "delivered"

    fresh = dao.get_task(root)
    assert fresh["state"] == "delivered"
    assert fresh["agent_ref"] is None  # placement cleared on completion

    status = orch.operations.orch_status({"project": "p"})
    assert status["complete"] is True
    assert status["counts"] == {"delivered": 1}
    assert status["escalations"] == []

    # The delivery + its artifact ref are recorded in append-only memory.
    mems = dao.list_attempt_memories(root)
    assert [m["outcome"] for m in mems] == ["delivered"]
    assert mems[0]["payload_ref"] == "artifact:result"


def test_empty_payload_fails_screen(orch):
    res = orch.operations.orch_plan_create({"project": "p", "goal": "do it"})
    root = res["task_id"]
    task = orch.DAO().get_task(root)
    reply = orch.operations.deliver({
        "task_id": root, "agent_ref": task["agent_ref"],
        "contract": res["contract"], "payload_ref": "   ",
    })
    assert reply["ok"] is False
    assert orch.DAO().get_task(root)["state"] == "active"  # unchanged


def test_wrong_agent_ref_is_rejected(orch):
    res = orch.operations.orch_plan_create({"project": "p", "goal": "do it"})
    root = res["task_id"]
    with pytest.raises(ValueError):
        orch.operations.deliver({
            "task_id": root, "agent_ref": "agent:imposter",
            "contract": res["contract"], "payload_ref": "artifact:x",
        })


def test_transient_failure_auto_retries_to_ready_then_redispatched(orch):
    res = orch.operations.orch_plan_create({"project": "p", "goal": "do it"})
    root = res["task_id"]
    dao = orch.DAO()
    task = dao.get_task(root)
    orch.operations.fail({
        "task_id": root, "agent_ref": task["agent_ref"],
        "reason_type": "transient-error", "reason_text": "agent died",
    })
    # transient -> ready (retry_count bumped) -> re-dispatched -> active again.
    fresh = dao.get_task(root)
    assert fresh["state"] == "active"
    assert fresh["retry_count"] == 1
