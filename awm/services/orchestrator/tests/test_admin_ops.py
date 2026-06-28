"""The attached-only DAG-restructuring ops (T3, orchestrator side): relocate a
node's funnel, add a dependency, link a repo. These are reached only through the
agents service's attach-gated relay; here we exercise the kernel mutations
directly (acyclicity, edge re-pointing, durable repos + the live-link seam)."""

from __future__ import annotations

import json

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.smoke]


def _open(orch, goal="work"):
    return orch.operations.orch_node_open({"project": "p", "goal": goal})


def test_relocate_repoints_funnel_to_new_consumer(orch):
    # A consumer to relocate UNDER (a second node, also upstream of root).
    other = _open(orch, "other")["task_id"]
    res = _open(orch, "movable")
    tid = res["task_id"]
    dao = orch.DAO()
    contract = dao.list_contracts_by_producer(tid)[0]
    root = dao.get_root()

    # Initially root consumes the movable node's deliverable.
    assert dao.list_consumers_of_contract(contract["id"]) == [root["id"]]

    agent_ref = dao.get_task(tid)["agent_ref"]
    out = orch.operations.relocate_task(
        {"task_id": tid, "agent_ref": agent_ref, "new_consumer_task": other})
    assert out["ok"] is True and out["edges_moved"] == 1
    # The old root edge is gone; `other` now consumes it.
    assert dao.list_consumers_of_contract(contract["id"]) == [other]


def test_relocate_defaults_to_root(orch):
    other = _open(orch, "other")["task_id"]
    res = _open(orch, "movable")
    tid = res["task_id"]
    dao = orch.DAO()
    contract = dao.list_contracts_by_producer(tid)[0]
    agent_ref = dao.get_task(tid)["agent_ref"]

    # First move it under `other`, then relocate with no consumer → back to root.
    orch.operations.relocate_task(
        {"task_id": tid, "agent_ref": agent_ref, "new_consumer_task": other})
    orch.operations.relocate_task({"task_id": tid, "agent_ref": agent_ref})
    assert dao.list_consumers_of_contract(contract["id"]) == [dao.get_root()["id"]]


def test_relocate_rejects_self_consumer(orch):
    tid = _open(orch)["task_id"]
    agent_ref = orch.DAO().get_task(tid)["agent_ref"]
    with pytest.raises(ValueError):
        orch.operations.relocate_task(
            {"task_id": tid, "agent_ref": agent_ref, "new_consumer_task": tid})


def test_node_add_dependency_by_name(orch):
    # producer publishes a contract; consumer depends on it via the admin op.
    prod = orch.operations.orch_task_attach({
        "project": "p", "goal": "make c1", "produces": [{"name": "c1"}]})
    consumer = _open(orch, "uses c1")
    ctid = consumer["task_id"]
    agent_ref = orch.DAO().get_task(ctid)["agent_ref"]

    out = orch.operations.node_add_dependency(
        {"task_id": ctid, "agent_ref": agent_ref, "contract": "c1"})
    assert out["ok"] is True
    deps = orch.DAO().depends_on(ctid)
    assert prod["task_id"] in deps


def test_node_add_dependency_rejects_cycle(orch):
    a = _open(orch, "a")
    atid = a["task_id"]
    # a's own deliverable; making a depend on it is a self-cycle.
    contract = orch.DAO().list_contracts_by_producer(atid)[0]
    agent_ref = orch.DAO().get_task(atid)["agent_ref"]
    with pytest.raises(ValueError):
        orch.operations.node_add_dependency(
            {"task_id": atid, "agent_ref": agent_ref,
             "contract_id": contract["id"]})


def test_link_repo_persists_and_calls_seam(orch):
    linked = {}
    orch.operations.configure(
        link_repos_fn=lambda project, slug, repos: linked.update(
            {"project": project, "slug": slug, "repos": repos}))
    tid = _open(orch)["task_id"]
    agent_ref = orch.DAO().get_task(tid)["agent_ref"]

    out = orch.operations.link_repo(
        {"task_id": tid, "agent_ref": agent_ref,
         "repo": {"name": "core", "project": "awm", "scope": "svc-agents"}})
    assert out["ok"] is True
    repos = json.loads(orch.DAO().get_task(tid)["repos"])
    assert repos == [{"name": "core", "project": "awm", "scope": "svc-agents"}]
    # The live re-link seam fired with the merged list.
    assert linked["repos"] == repos
    assert linked["slug"] == orch.DAO().get_task(tid)["workspace_slug"]


def test_link_repo_merges_dedup_by_name(orch):
    orch.operations.configure(link_repos_fn=lambda *a, **k: None)
    tid = _open(orch)["task_id"]
    agent_ref = orch.DAO().get_task(tid)["agent_ref"]
    orch.operations.link_repo(
        {"task_id": tid, "agent_ref": agent_ref, "repo": "alpha"})
    orch.operations.link_repo(
        {"task_id": tid, "agent_ref": agent_ref, "repo": "beta"})
    # Re-link alpha with a different target — dedup by name keeps one alpha.
    orch.operations.link_repo(
        {"task_id": tid, "agent_ref": agent_ref,
         "repo": {"name": "alpha", "scope": "alpha2"}})
    repos = json.loads(orch.DAO().get_task(tid)["repos"])
    names = sorted(r["name"] for r in repos)
    assert names == ["alpha", "beta"]
    alpha = next(r for r in repos if r["name"] == "alpha")
    assert alpha["scope"] == "alpha2"
