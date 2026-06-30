"""Placement harness / model / effort resolution at the dispatch seam.

The orchestrator pins an explicit, cheap model for a claude placement so it never
silently inherits the human's *interactive* CLI default (e.g. Opus / high). The
env vars (``AWM_PLACEMENT_HARNESS`` / ``_MODEL`` / ``_EFFORT``) are the single
lever for every node; opencode keeps its own ``None``-model default (DSv4-free).
"""

from __future__ import annotations

import pytest

from awm.orchestrator import dispatch

pytestmark = [pytest.mark.unit, pytest.mark.smoke]


def _clear_placement_env(monkeypatch):
    for var in ("AWM_PLACEMENT_HARNESS", "AWM_PLACEMENT_MODEL",
                "AWM_PLACEMENT_EFFORT"):
        monkeypatch.delenv(var, raising=False)


def test_attended_claude_pins_cheap_model(orch, monkeypatch):
    # No env config: an attended node defaults to claude, which MUST be pinned to
    # the explicit cheap default rather than the interactive CLI default.
    _clear_placement_env(monkeypatch)
    res = orch.operations.orch_node_open({"goal": "x"})
    p = orch.placements[(res["task_id"], "worker")]
    assert p["harness"] == "claude"
    assert p["model"] == dispatch.DEFAULT_CLAUDE_PLACEMENT_MODEL
    assert p["effort"] == dispatch.DEFAULT_CLAUDE_PLACEMENT_EFFORT
    assert p["attached"] is True


def test_opencode_harness_leaves_model_unset(orch, monkeypatch):
    # The sandbox pins opencode → no model is forced, so the opencode backend's
    # own DSv4-free default wins. effort is claude-only and stays absent.
    _clear_placement_env(monkeypatch)
    monkeypatch.setenv("AWM_PLACEMENT_HARNESS", "opencode")
    res = orch.operations.orch_node_open({"goal": "x"})
    p = orch.placements[(res["task_id"], "worker")]
    assert p["harness"] == "opencode"
    assert "model" not in p
    assert "effort" not in p


def test_env_pins_override_the_default(orch, monkeypatch):
    _clear_placement_env(monkeypatch)
    monkeypatch.setenv("AWM_PLACEMENT_HARNESS", "claude")
    monkeypatch.setenv("AWM_PLACEMENT_MODEL", "sonnet")
    monkeypatch.setenv("AWM_PLACEMENT_EFFORT", "high")
    res = orch.operations.orch_node_open({"goal": "x"})
    p = orch.placements[(res["task_id"], "worker")]
    assert p["harness"] == "claude"
    assert p["model"] == "sonnet"
    assert p["effort"] == "high"


def test_unattended_no_env_omits_harness_and_model(orch, monkeypatch):
    # An unattended node with no env config leaves harness/model/effort OUT of the
    # payload, so the agents side applies its own opencode default. (Both public
    # create ops are attended, so build the payload directly for this branch.)
    _clear_placement_env(monkeypatch)
    dao = orch.DAO()
    tid = dao.create_task("x", state="plan_approved")
    payload = dispatch._build_payload(dao, dao.get_task(tid), "worker", "orch-x")
    assert "harness" not in payload
    assert "model" not in payload
    assert "effort" not in payload
    assert "attached" not in payload
