"""Periodic reconcile (T5) — the boot recovery pair re-run on a ~60s sweep, with
the guards that don't exist at boot: MIN-AGE, TWO-STRIKE, and LIVENESS-UNKNOWN.

Boot recovery is unconditional (a full restart definitely killed every
placement). A periodic sweep against a LIVE system must be gentler: a node may
be mid-spawn (MIN-AGE), an agents restart may briefly report emptiness
(TWO-STRIKE), or agents may be unreachable (LIVENESS-UNKNOWN). The frontier
re-scan half is cheap + idempotent and runs every sweep.
"""

from __future__ import annotations

import time

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.smoke]


def _active_task(orch):
    """An out-state node (state ``active`` / mode ``worker``) with a slug."""
    res = orch.operations.orch_node_open({"goal": "build a thing"})
    return res["task_id"], orch.DAO().get_task(res["task_id"])


# ---------------------------------------------------------------------------
# scan_orphans — MIN-AGE guard
# ---------------------------------------------------------------------------

def test_min_age_protects_the_spawn_window(orch):
    tid, task = _active_task(orch)
    now = int(task["updated_at"])
    # Fresh row (age 0) with no live session: NOT yet considered orphaned.
    fresh = orch.kernel.scan_orphans(orch.DAO(), set(), min_age_s=120, now_ts=now)
    assert tid not in fresh
    # The same row, 200s later: now old enough to be an orphan candidate.
    aged = orch.kernel.scan_orphans(orch.DAO(), set(), min_age_s=120,
                                    now_ts=now + 200)
    assert tid in aged


def test_live_placement_is_never_orphaned(orch):
    tid, task = _active_task(orch)
    now = int(task["updated_at"])
    seen = orch.kernel.scan_orphans(
        orch.DAO(), {task["workspace_slug"]}, min_age_s=0, now_ts=now + 999)
    assert tid not in seen                    # its unit is live


def test_scan_unknown_liveness_is_none(orch):
    _active_task(orch)
    assert orch.kernel.scan_orphans(orch.DAO(), None, min_age_s=0) is None


# ---------------------------------------------------------------------------
# periodic_recover — TWO-STRIKE guard
# ---------------------------------------------------------------------------

def test_two_strike_requires_two_consecutive_sweeps(orch):
    tid, task = _active_task(orch)
    # Pin now_ts past the task's updated_at so the min-age check is deterministic
    # (independent of wall-clock resolution / any clock skew).
    now = int(task["updated_at"]) + 1000

    # First sweep: seen orphaned, but prev was empty → NOT recovered yet.
    recovered, current = orch.kernel.periodic_recover(
        orch.DAO(), set(), set(), min_age_s=0, now_ts=now)
    assert recovered == set()
    assert tid in current
    assert orch.DAO().get_task(tid)["state"] == "active"   # untouched

    # Second consecutive sweep with the same row orphaned → recovered now.
    recovered2, current2 = orch.kernel.periodic_recover(
        orch.DAO(), set(), current, min_age_s=0, now_ts=now)
    assert tid in recovered2
    reset = orch.DAO().get_task(tid)
    assert reset["state"] == "plan_approved"               # worker leg's resting state
    assert reset["mode"] is None
    assert reset["agent_ref"] is None
    assert reset["workspace_slug"] == task["workspace_slug"]  # unit retained


def test_transient_gap_between_sweeps_resets_the_strike(orch):
    tid, task = _active_task(orch)
    now = int(task["updated_at"]) + 1000
    # Sweep 1: orphaned.
    _, current = orch.kernel.periodic_recover(
        orch.DAO(), set(), set(), min_age_s=0, now_ts=now)
    assert tid in current
    # Sweep 2: the unit reports live again (the placement recovered on its own) →
    # not orphaned this sweep, so the strike is dropped and nothing is reset.
    recovered, current2 = orch.kernel.periodic_recover(
        orch.DAO(), {task["workspace_slug"]}, current, min_age_s=0, now_ts=now)
    assert recovered == set()
    assert tid not in current2
    assert orch.DAO().get_task(tid)["state"] == "active"


# ---------------------------------------------------------------------------
# periodic_recover — LIVENESS-UNKNOWN guard
# ---------------------------------------------------------------------------

def test_liveness_unknown_is_a_noop_and_preserves_memory(orch):
    tid, _ = _active_task(orch)
    # Even with a prior strike, unknown liveness recovers nothing and KEEPS the
    # two-strike memory (so a real outage doesn't erase a pending strike).
    recovered, current = orch.kernel.periodic_recover(
        orch.DAO(), None, {tid}, min_age_s=0)
    assert recovered == set()
    assert current == {tid}
    assert orch.DAO().get_task(tid)["state"] == "active"


# ---------------------------------------------------------------------------
# Frontier re-scan — idempotent, runs every sweep
# ---------------------------------------------------------------------------

def test_frontier_rescan_is_idempotent(orch):
    tid = orch.DAO().create_task("x", state="ready")
    # First re-scan places the plan leg → the node flips to the out-state.
    orch.dispatch.enqueue(orch.kernel.reconcile(orch.DAO()))
    assert (tid, "plan") in orch.placements
    assert orch.DAO().get_task(tid)["state"] == "planning"
    # A second re-scan (the next sweep) does NOT re-emit it: it's an out-state now,
    # so reconcile omits it (and _prepare's rest-state re-check would skip anyway).
    intents = orch.kernel.reconcile(orch.DAO())
    assert (tid, "plan") not in intents


def test_recovered_node_is_redispatched_by_the_frontier_rescan(orch):
    # End-to-end within the kernel: a two-strike-confirmed orphan is reset to
    # resting, and the very next frontier re-scan re-dispatches its leg onto the
    # retained unit — recovery within one sweep.
    tid, task = _active_task(orch)
    now = int(task["updated_at"]) + 1000
    _, current = orch.kernel.periodic_recover(
        orch.DAO(), set(), set(), min_age_s=0, now_ts=now)
    orch.placements.clear()
    orch.kernel.periodic_recover(
        orch.DAO(), set(), current, min_age_s=0, now_ts=now)
    assert orch.DAO().get_task(tid)["state"] == "plan_approved"
    orch.dispatch.enqueue(orch.kernel.reconcile(orch.DAO()))
    assert (tid, "worker") in orch.placements
    again = orch.DAO().get_task(tid)
    assert again["state"] == "active"
    assert again["workspace_slug"] == task["workspace_slug"]
