"""Uniform placement model (T5) — the dispatch payload no longer forks on
attach state.

Every placement — attended or detached — defaults to the SAME driver
(claude / haiku / medium), supplied by the agents-side driver config
(``place_on_task._resolve_driver``), NOT by dispatch. Dispatch only threads
explicit ``AWM_PLACEMENT_*`` env overrides; when none are set the payload omits
harness/model/effort entirely (deferred to the driver-config default). There is
no second-class detached node. opencode stays available purely as an explicit
override.
"""

from __future__ import annotations

import pytest

from awm.orchestrator import dispatch

pytestmark = [pytest.mark.unit, pytest.mark.smoke]


def _clear_placement_env(monkeypatch):
    for var in ("AWM_PLACEMENT_HARNESS", "AWM_PLACEMENT_MODEL",
                "AWM_PLACEMENT_EFFORT"):
        monkeypatch.delenv(var, raising=False)


def test_attended_no_env_defers_to_driver_config(orch, monkeypatch):
    # No env config: the payload carries NO harness/model/effort — the uniform
    # driver-config default (claude/haiku/medium) applies agents-side. The only
    # attach-derived field is the supervisor-freeze intent.
    _clear_placement_env(monkeypatch)
    res = orch.operations.orch_node_open({"goal": "x"})
    p = orch.placements[(res["task_id"], "worker")]
    assert "harness" not in p
    assert "model" not in p
    assert "effort" not in p
    assert p["attached"] is True   # freeze intent still threaded (orthogonal)


def test_no_attach_fork_detached_matches_attended(orch, monkeypatch):
    # The whole point: a detached node's payload is driver-wise IDENTICAL to an
    # attended one (no attached→claude / detached→opencode fork). Build a detached
    # worker payload directly (both public create ops are attended).
    _clear_placement_env(monkeypatch)
    dao = orch.DAO()
    tid = dao.create_task("x", state="plan_approved")
    payload = dispatch._build_payload(dao, dao.get_task(tid), "worker", "orch-x")
    assert "harness" not in payload
    assert "model" not in payload
    assert "effort" not in payload
    assert "attached" not in payload   # detached — no freeze intent either


def test_env_override_threads_through(orch, monkeypatch):
    # Explicit env overrides still win and are threaded onto the payload (they
    # beat the driver-config default in _resolve_driver). opencode is a valid
    # explicit override.
    _clear_placement_env(monkeypatch)
    monkeypatch.setenv("AWM_PLACEMENT_HARNESS", "opencode")
    monkeypatch.setenv("AWM_PLACEMENT_MODEL", "some-model")
    monkeypatch.setenv("AWM_PLACEMENT_EFFORT", "high")
    res = orch.operations.orch_node_open({"goal": "x"})
    p = orch.placements[(res["task_id"], "worker")]
    assert p["harness"] == "opencode"
    assert p["model"] == "some-model"
    assert p["effort"] == "high"


def test_partial_env_override_only_threads_what_is_set(orch, monkeypatch):
    # Only the set env vars ride the payload; the rest defer to the driver config.
    _clear_placement_env(monkeypatch)
    monkeypatch.setenv("AWM_PLACEMENT_MODEL", "sonnet")
    res = orch.operations.orch_node_open({"goal": "x"})
    p = orch.placements[(res["task_id"], "worker")]
    assert "harness" not in p
    assert p["model"] == "sonnet"
    assert "effort" not in p


def test_old_default_constants_are_gone(monkeypatch):
    # The dispatch-side default constants moved to the agents driver config.
    assert not hasattr(dispatch, "DEFAULT_CLAUDE_PLACEMENT_MODEL")
    assert not hasattr(dispatch, "DEFAULT_CLAUDE_PLACEMENT_EFFORT")
