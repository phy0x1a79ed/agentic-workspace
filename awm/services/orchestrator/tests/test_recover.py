"""Boot orphan recovery.

A full gateway restart bounces the agents service too, which closes every open
agent instance — so a task left in an *out* state (``planning`` /
``verifying_plan`` / ``active`` / ``decomposing``) has a dead placement and no one
left to report it. :func:`kernel.recover_orphans` resets such a node to its
resting predecessor so the boot :func:`kernel.reconcile` sweep re-dispatches the
same leg onto the retained workspace unit. A still-live placement is left
untouched (no double-spawn); an *unknown* signal (agents unreachable) is a no-op.
"""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.smoke]


def _active_task(orch):
    """A genuine out-state node: an attended worker placed directly via the
    drop-in op (state ``active``, mode ``worker``)."""
    res = orch.operations.orch_node_open({"goal": "build a thing"})
    return res["task_id"], orch.DAO().get_task(res["task_id"])


def test_orphan_is_reset_and_redispatched(orch):
    tid, task = _active_task(orch)
    assert task["state"] == "active"
    slug = task["workspace_slug"]

    # Restart with NO live sessions → the placement is orphaned and recovered.
    orch.kernel.recover_orphans(orch.DAO(), set())
    reset = orch.DAO().get_task(tid)
    assert reset["state"] == "plan_approved"  # the worker leg's resting state
    assert reset["mode"] is None
    assert reset["agent_ref"] is None
    assert reset["placement_token"] is None
    assert reset["workspace_slug"] == slug  # unit retained for the resume

    # The boot reconcile sweep re-dispatches the same leg onto the same unit.
    orch.placements.clear()
    orch.dispatch.enqueue(orch.kernel.reconcile(orch.DAO()))
    assert (tid, "worker") in orch.placements
    again = orch.DAO().get_task(tid)
    assert again["state"] == "active"
    assert again["workspace_slug"] == slug
    # Attached persisted on the row → the fresh placement is re-marked attached.
    assert orch.placements[(tid, "worker")]["attached"] is True


def test_live_placement_is_left_untouched(orch):
    tid, task = _active_task(orch)
    # The agents service still reports a session on this unit → not orphaned.
    orch.kernel.recover_orphans(orch.DAO(), {task["workspace_slug"]})
    after = orch.DAO().get_task(tid)
    assert after["state"] == "active"
    assert after["mode"] == "worker"
    assert after["agent_ref"] == task["agent_ref"]
    assert after["placement_token"] == task["placement_token"]


def test_unknown_signal_is_a_noop(orch):
    tid, task = _active_task(orch)
    # None = agents service unreachable at boot → conservative no-op.
    orch.kernel.recover_orphans(orch.DAO(), None)
    after = orch.DAO().get_task(tid)
    assert after["state"] == "active"
    assert after["mode"] == "worker"
    assert after["agent_ref"] == task["agent_ref"]


def test_decomposing_orphan_recovers_to_decompose_pending(orch):
    # A different out-state / mode: orch_task_create births an attended node into
    # decompose_pending and places a PLANNER → state decomposing, mode planner.
    res = orch.operations.orch_task_create({"goal": "spec me"})
    tid = res["task_id"]
    assert orch.DAO().get_task(tid)["state"] == "decomposing"

    orch.kernel.recover_orphans(orch.DAO(), set())
    reset = orch.DAO().get_task(tid)
    assert reset["state"] == "decompose_pending"  # planner leg's resting state
    assert reset["mode"] is None
    assert reset["agent_ref"] is None
