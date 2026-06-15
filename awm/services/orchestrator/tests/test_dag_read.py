"""``orch_dag`` — the one-shot plan read for a UI.

Builds a small DAG with a *shared* dependency (one producer, multiple
consumers — the case a tree model can't represent) and asserts the snapshot is
coherent: tasks (incl. the root sentinel), contracts with delivery flags, and
denormalized edges carrying both endpoints + the contract label. Also pins the
manifest/handler wiring (public + MCP-visible, unlike the privileged ops).
"""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.unit]


def _attach(orch, *, goal, produces, depends_on=()):
    return orch.operations.orch_task_attach({
        "project": "p", "goal": goal,
        "produces": [{"name": n, "spec": s} for (n, s) in produces],
        "depends_on": list(depends_on),
    })


def _build_shared_dag(orch):
    """A produces ``shared``; B and C both depend on it. So ``shared``'s producer
    (A) has multiple downstream consumers — a true DAG, not a tree."""
    a = _attach(orch, goal="produce shared", produces=(("shared", "the input"),))
    b = _attach(orch, goal="consume in B", produces=(("b_out", ""),),
                depends_on=("shared",))
    c = _attach(orch, goal="consume in C", produces=(("c_out", ""),),
                depends_on=("shared",))
    return a["task_id"], b["task_id"], c["task_id"]


def test_dag_returns_tasks_contracts_edges(orch):
    a, b, c = _build_shared_dag(orch)
    root_id = orch.DAO().get_root()["id"]

    dag = orch.operations.orch_dag({})  # global

    assert dag["root_id"] == root_id
    task_ids = {t["task_id"] for t in dag["tasks"]}
    assert {a, b, c, root_id} <= task_ids
    # The root sentinel is present and flagged.
    root_row = next(t for t in dag["tasks"] if t["task_id"] == root_id)
    assert root_row["is_root"] is True

    # Contracts carry producer + a delivery flag (undelivered at this point).
    shared = next(ct for ct in dag["contracts"] if ct["name"] == "shared")
    assert shared["producer_task"] == a
    assert shared["delivered"] is False

    # Edges are denormalized: each row resolves contract_name + producer_task.
    shared_edges = [e for e in dag["edges"] if e["contract_name"] == "shared"]
    # shared is consumed by root, B, and C — one producer, three consumers.
    assert all(e["producer_task"] == a for e in shared_edges)
    assert {e["consumer_task"] for e in shared_edges} == {root_id, b, c}
    assert all(e["delivered"] is False for e in shared_edges)


def test_dag_reflects_delivery(orch):
    a, b, c = _build_shared_dag(orch)
    # A real contract is delivered from the worker leg: drive A through
    # plan→verify→approve to ``active``, then read its worker placement's
    # agent_ref (a freshly-attached task rests in ``planning``, not ``active``).
    orch.advance_to_active(a)
    task_a = orch.DAO().get_task(a)

    orch.operations.deliver({
        "task_id": a, "agent_ref": task_a["agent_ref"],
        "contract": "shared", "payload_ref": "artifact:shared",
    })

    dag = orch.operations.orch_dag({})
    shared = next(ct for ct in dag["contracts"] if ct["name"] == "shared")
    assert shared["delivered"] is True
    assert shared["payload_ref"] == "artifact:shared"
    assert all(
        e["delivered"] is True
        for e in dag["edges"] if e["contract_name"] == "shared"
    )


def test_dag_project_filter(orch):
    _build_shared_dag(orch)
    dag = orch.operations.orch_dag({"project": "p"})
    # All real work is in project p; tasks list excludes the _root sentinel.
    assert all(t["is_root"] is False for t in dag["tasks"])
    # root_id is still reported so the client can special-case the sentinel.
    assert dag["root_id"] == orch.DAO().get_root()["id"]


def test_orch_dag_is_public_and_wired():
    from awm.orchestrator import hub_adapter as ha

    fn_names = {f["name"] for f in ha.API_MANIFEST["functions"]}
    assert "orch_dag" in fn_names            # MCP-visible (public read)
    assert "orch_dag" in ha.HANDLERS         # dispatchable
    # Privileged ops stay omitted from the manifest.
    assert {"deliver", "fail", "decompose_commit"} & fn_names == set()
