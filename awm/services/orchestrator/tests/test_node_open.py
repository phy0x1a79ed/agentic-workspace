"""Seamless drop-in: ``orch_node_open`` creates a node and places an attended
worker on it directly, skipping the plan → verify → planner legs.

The drop-in counterpart to ``orch_task_create`` (which dispatches a planner to
specify the task first). A node opened this way is born ``plan_approved`` so the
dispatch machinery places a WORKER immediately; it is human-``attached`` (so its
placement payload carries the claude harness); its synthetic deliverable hangs
the node upstream of root; and an optional ``repo`` rides the placement payload
as the workspace ``repos`` link.
"""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.smoke]


def test_open_places_attended_worker_directly(orch):
    res = orch.operations.orch_node_open({"goal": "chat + build"})
    tid = res["task_id"]
    dao = orch.DAO()
    task = dao.get_task(tid)

    # A WORKER was placed directly — no plan/verify/planner leg ran.
    assert (tid, "worker") in orch.placements
    assert (tid, "plan") not in orch.placements
    assert (tid, "planner") not in orch.placements
    assert task["state"] == "active"
    assert task["mode"] == "worker"
    # Attended, with the worker on the claude harness (interactive terminal).
    assert bool(task["attached"]) is True
    assert orch.placements[(tid, "worker")]["harness"] == "claude"

    # The handles the contract promises are returned (slug is the attach key).
    assert res["workspace_slug"] == task["workspace_slug"]
    assert res["agent_ref"] == task["agent_ref"]  # set inline under sync dispatch

    # It hangs in the DAG: root depends on its synthetic deliverable.
    root = dao.get_root()
    assert res["consumer"] == root["id"]
    deliverable = dao.list_contracts_by_producer(tid)[0]["name"]
    assert deliverable.startswith("deliverable:")


def test_open_threads_repo_into_placement(orch):
    res = orch.operations.orch_node_open({
        "goal": "work on the agents svc",
        "repo": {"name": "core", "project": "awm", "scope": "svc-agents"},
    })
    tid = res["task_id"]
    expected = [{"name": "core", "project": "awm", "scope": "svc-agents"}]
    # Attached as a first-class task_scopes row (the scope's own coordinates)...
    repos = [{"name": r["name"], "project": r["project"], "scope": r["scope"]}
             for r in orch.DAO().list_task_scopes(tid)]
    assert repos == expected
    # ...and threaded into the worker placement payload for the workspace link.
    assert orch.placements[(tid, "worker")]["repos"] == expected


def test_open_accepts_scope_ref_string_repo(orch):
    # A "project/scope" string names the scope's own coordinate (a task has no
    # project of its own to default a bare scope to).
    res = orch.operations.orch_node_open({"goal": "x", "repo": "awm/my-scope"})
    repos = [{"name": r["name"], "project": r["project"], "scope": r["scope"]}
             for r in orch.DAO().list_task_scopes(res["task_id"])]
    assert repos == [{"name": "my-scope", "project": "awm", "scope": "my-scope"}]


def test_open_runs_to_completion(orch):
    res = orch.operations.orch_node_open({"goal": "ship it"})
    tid = res["task_id"]
    dao = orch.DAO()
    task = dao.get_task(tid)
    deliverable = dao.list_contracts_by_producer(tid)[0]["name"]
    orch.operations.deliver({
        "task_id": tid, "agent_ref": task["agent_ref"],
        "contract": deliverable, "payload_ref": "artifact:done",
    })
    assert dao.get_task(tid)["state"] == "completed"
    assert dao.get_root()["state"] == "completed"
