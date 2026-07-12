"""The opt-in, execution-verified acceptance gate — the plan→verify leg mirrored
one layer later.

A GATED worker delivery (any produced contract carries an ``accept_spec``) sets
the contract ``payload_ref`` as a CLAIM but NOT ``delivered_ts`` — so nothing
completes and no consumer advances. The task rests ``work_delivered`` and an
independent accept verifier (``accept`` mode) is dispatched; only ``accept_work``
promotes the claims to real deliveries. ``reject_work`` loops back to the worker
(spending ``accept_budget``) with a rework reason, and — once the budget is out —
rests the task ``failed`` (``contract-unsatisfiable``), routing its consumers.

Strictly opt-in: a task attached WITHOUT ``accept`` has NULL ``accept_spec`` and
takes the unchanged legacy immediate-completion path.
"""

from __future__ import annotations

import json

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.smoke]

SPEC = {
    "objective": "the widget builds and its smoke test passes",
    "checks": [
        {"name": "builds", "cmd": "make build", "expect_exit": 0},
        {"name": "smoke", "cmd": "./smoke.sh", "expect_exit": 0,
         "expect_output": "OK"},
    ],
}


def _attach_gated(orch, *, goal="build the widget",
                  produces=(("c1", "the deliverable"),), accept=SPEC,
                  depends_on=None):
    """Attach a gated leaf task upstream of root (accept_spec on its contracts)."""
    args = {
        "goal": goal,
        "produces": [{"name": n, "spec": s} for (n, s) in produces],
    }
    if accept is not None:
        args["accept"] = accept
    if depends_on is not None:
        args["depends_on"] = depends_on
    return orch.operations.orch_task_attach(args)


def _deliver_claim(orch, tid, *, contract="c1", payload_ref="artifact:v1"):
    """Drive a gated task active and deliver a claim on ``contract``."""
    orch.advance_to_active(tid)
    task = orch.DAO().get_task(tid)
    return orch.operations.deliver({
        "task_id": tid, "agent_ref": task["agent_ref"],
        "contract": contract, "payload_ref": payload_ref,
    })


# ---------------------------------------------------------------------------
# Gated delivery is a claim, not a completion
# ---------------------------------------------------------------------------


def test_accept_spec_is_stored_on_produced_contracts(orch):
    res = _attach_gated(orch)
    dao = orch.DAO()
    contracts = dao.list_contracts_by_producer(res["task_id"])
    assert contracts and all(c["accept_spec"] for c in contracts)
    assert json.loads(contracts[0]["accept_spec"]) == SPEC


def test_gated_deliver_is_a_claim_not_completion(orch):
    res = _attach_gated(orch)
    tid = res["task_id"]
    dao = orch.DAO()
    reply = _deliver_claim(orch, tid)

    # A claim: ok, but NOT completed — the task rests in work_delivered and then
    # flips to verifying_work as the accept verifier is placed (sync dispatch).
    assert reply["ok"] is True
    assert reply.get("claimed") is True
    t = dao.get_task(tid)
    assert t["state"] == "verifying_work"
    assert t["mode"] == "accept"

    # The contract is CLAIMED: payload_ref set, delivered_ts still NULL.
    c = dao.get_contract_by_name("c1")
    assert c["payload_ref"] == "artifact:v1"
    assert c["delivered_ts"] is None

    # Root did NOT complete on a claim (its prerequisite is not delivered).
    root = dao.get_root()
    assert root["state"] != "completed"


def test_accept_placement_carries_spec_produces_and_claim_prereadings(orch):
    res = _attach_gated(orch)
    tid = res["task_id"]
    _deliver_claim(orch, tid, payload_ref="artifact:claim")
    payload = orch.placements[(tid, "accept")]
    assert payload["mode"] == "accept"
    assert payload["contracts_out"] == []  # the verifier delivers nothing
    brief = json.loads(payload["brief"])
    assert brief["accept_spec"] == SPEC
    assert "c1" in brief["produces"]
    # The claimed artifact is materialized read-only for the verifier to check.
    assert {"name": "c1", "path": "artifact:claim"} in payload["prereadings"]


def test_partial_claims_stay_active_until_all_claimed(orch):
    """A multi-contract gated task rests active until EVERY produced contract is
    claimed, then flips to work_delivered/verifying_work."""
    res = _attach_gated(orch, produces=(("c1", "a"), ("c2", "b")))
    tid = res["task_id"]
    orch.advance_to_active(tid)
    task = orch.DAO().get_task(tid)

    r1 = orch.operations.deliver({
        "task_id": tid, "agent_ref": task["agent_ref"],
        "contract": "c1", "payload_ref": "artifact:c1"})
    assert r1["ok"] is True
    assert orch.DAO().get_task(tid)["state"] == "active"  # c2 not claimed yet

    r2 = orch.operations.deliver({
        "task_id": tid, "agent_ref": task["agent_ref"],
        "contract": "c2", "payload_ref": "artifact:c2"})
    assert r2["ok"] is True
    assert orch.DAO().get_task(tid)["state"] == "verifying_work"


# ---------------------------------------------------------------------------
# accept_work completes; reject_work loops to the worker
# ---------------------------------------------------------------------------


def test_accept_work_completes_and_promotes_claims(orch):
    res = _attach_gated(orch)
    tid = res["task_id"]
    _deliver_claim(orch, tid, payload_ref="artifact:final")
    dao = orch.DAO()

    reply = orch.operations.accept_work(
        {"task_id": tid, "evidence": "all checks pass"})
    assert reply["ok"] is True
    assert reply["state"] == "completed"

    # The claim is now a real delivery.
    c = dao.get_contract_by_name("c1")
    assert c["delivered_ts"] is not None
    assert c["payload_ref"] == "artifact:final"

    # Task completed, placement cleared, unit retained; root completes too.
    fresh = dao.get_task(tid)
    assert fresh["state"] == "completed"
    assert fresh["agent_ref"] is None
    assert fresh["workspace_slug"] is not None
    assert dao.get_root()["state"] == "completed"

    # The acceptance is recorded in append-only memory.
    mems = dao.list_attempt_memories(tid)
    assert mems[-1]["outcome"] == "accepted"
    assert mems[-1]["reason_type"] == "work-accepted"


def test_accept_work_rejects_wrong_state(orch):
    res = _attach_gated(orch)
    tid = res["task_id"]
    orch.advance_to_active(tid)  # active, not verifying_work
    reply = orch.operations.accept_work({"task_id": tid})
    assert reply["ok"] is False
    assert "verifying_work" in reply["error"]


def test_reject_work_loops_to_worker_and_spends_budget(orch):
    res = _attach_gated(orch)
    tid = res["task_id"]
    dao = orch.DAO()
    budget0 = dao.get_task(tid)["accept_budget"]
    _deliver_claim(orch, tid, payload_ref="artifact:bad")
    orch.placements.clear()

    reply = orch.operations.reject_work(
        {"task_id": tid, "reason": "smoke check failed: got FAIL"})
    assert reply["ok"] is True

    t = dao.get_task(tid)
    # Back at the worker leg (re-dispatched → active), one accept-budget spent.
    assert t["state"] == "active"
    assert t["mode"] == "worker"
    assert t["accept_budget"] == budget0 - 1

    # The claim was cleared (undelivered) so the rework starts clean.
    c = dao.get_contract_by_name("c1")
    assert c["payload_ref"] is None
    assert c["delivered_ts"] is None

    # The rework reason surfaces in the next worker payload (why + partial ref).
    payload = orch.placements[(tid, "worker")]
    brief = json.loads(payload["brief"])
    assert brief["rework_reason"]["reason_text"] == "smoke check failed: got FAIL"
    # The read-only authoritative acceptance criteria ride the worker brief too.
    assert brief["accept_spec"] == SPEC


def test_reject_loop_is_bounded_then_fails_and_routes_consumers(orch):
    # A downstream consumer B depends on the gated task A's contract so we can
    # observe consumer routing when A exhausts its accept budget.
    res_a = _attach_gated(orch, goal="produce c1")
    tid_a = res_a["task_id"]
    res_b = orch.operations.orch_task_attach({
        "goal": "consume c1",
        "produces": [{"name": "c2", "spec": "downstream"}],
        "depends_on": ["c1"],
    })
    tid_b = res_b["task_id"]
    dao = orch.DAO()
    assert dao.get_task(tid_b)["state"] == "blocked"  # waits on c1

    # accept_budget starts at 2 → three rejects exhaust it. Each reject after a
    # fresh claim: deliver → verifying_work → reject_work.
    for i in range(3):
        _deliver_claim(orch, tid_a, payload_ref=f"artifact:try{i}")
        assert dao.get_task(tid_a)["state"] == "verifying_work"
        orch.operations.reject_work(
            {"task_id": tid_a, "reason": f"still failing ({i})"})

    # Budget out → the gate is a stalemate: A rests failed, its consumers route.
    a = dao.get_task(tid_a)
    assert a["state"] == "failed"
    # B (a real consumer of c1) re-enters decompose_pending to re-plan A's slot
    # (then sync dispatch places its planner → decomposing); root (also a
    # consumer) escalates to abandoned.
    assert dao.get_task(tid_b)["state"] == "decomposing"
    assert dao.get_root()["state"] == "abandoned"


# ---------------------------------------------------------------------------
# Backward compatibility — no accept ⇒ ungated legacy path
# ---------------------------------------------------------------------------


def test_ungated_task_takes_legacy_completion_path(orch):
    res = _attach_gated(orch, accept=None)  # no accept ⇒ NULL accept_spec
    tid = res["task_id"]
    dao = orch.DAO()
    assert dao.get_contract_by_name("c1")["accept_spec"] is None

    orch.advance_to_active(tid)
    task = dao.get_task(tid)
    reply = orch.operations.deliver({
        "task_id": tid, "agent_ref": task["agent_ref"],
        "contract": "c1", "payload_ref": "artifact:done"})
    # Legacy path: delivery completes the task immediately (no claim / gate).
    assert reply["ok"] is True
    assert reply.get("claimed") is None
    assert reply["state"] == "completed"
    assert dao.get_contract_by_name("c1")["delivered_ts"] is not None
    assert dao.get_root()["state"] == "completed"


# ---------------------------------------------------------------------------
# Recovery — a dead accept placement is recovered and re-emitted
# ---------------------------------------------------------------------------


def test_verifying_work_orphan_recovers_and_reemits_accept(orch):
    res = _attach_gated(orch)
    tid = res["task_id"]
    _deliver_claim(orch, tid)
    dao = orch.DAO()
    task = dao.get_task(tid)
    assert task["state"] == "verifying_work"
    slug = task["workspace_slug"]

    # Restart with NO live sessions → the accept placement is orphaned & reset.
    orch.kernel.recover_orphans(dao, set())
    reset = dao.get_task(tid)
    assert reset["state"] == "work_delivered"  # the accept leg's resting state
    assert reset["mode"] is None
    assert reset["agent_ref"] is None
    assert reset["workspace_slug"] == slug  # unit retained for the resume

    # The boot reconcile sweep re-dispatches the accept leg onto the same unit.
    orch.placements.clear()
    orch.dispatch.enqueue(orch.kernel.reconcile(dao))
    assert (tid, "accept") in orch.placements
    assert dao.get_task(tid)["state"] == "verifying_work"


def test_orch_retry_resets_accept_budget(orch):
    res = _attach_gated(orch)
    tid = res["task_id"]
    dao = orch.DAO()
    # Spend accept budget down to a stalemate (failed).
    for i in range(3):
        _deliver_claim(orch, tid, payload_ref=f"artifact:try{i}")
        orch.operations.reject_work({"task_id": tid, "reason": f"no ({i})"})
    assert dao.get_task(tid)["state"] == "failed"
    assert dao.get_task(tid)["accept_budget"] == 0

    reply = orch.operations.orch_retry({"task_id": tid})
    assert reply["ok"] is True
    fresh = dao.get_task(tid)
    # retry rests it ready then sync dispatch re-drives the PLAN leg → planning.
    assert fresh["state"] == "planning"
    assert fresh["accept_budget"] == 2  # fresh grant alongside replan_budget
    assert fresh["replan_budget"] == 2


# ---------------------------------------------------------------------------
# Migration v5 → v6 (additive columns; old rows survive)
# ---------------------------------------------------------------------------

_V5_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    goal TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    tags TEXT NOT NULL DEFAULT '[]',
    state TEXT NOT NULL DEFAULT 'blocked',
    is_root INTEGER NOT NULL DEFAULT 0,
    paused INTEGER NOT NULL DEFAULT 0,
    mode TEXT, workspace_slug TEXT, agent_ref TEXT, placement_token TEXT,
    plan_ref TEXT, attached INTEGER NOT NULL DEFAULT 0,
    steer_user_done INTEGER NOT NULL DEFAULT 0,
    steer_agent_ready INTEGER NOT NULL DEFAULT 0,
    steer_requested INTEGER NOT NULL DEFAULT 0,
    objective TEXT NOT NULL DEFAULT '',
    replan_budget INTEGER NOT NULL DEFAULT 2,
    retry_count INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS contracts (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, spec TEXT NOT NULL DEFAULT '',
    producer_task TEXT NOT NULL, payload_ref TEXT, delivered_ts INTEGER,
    created_at INTEGER NOT NULL
);
"""


def test_migration_v5_to_v6_adds_columns_and_keeps_old_rows(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    awm_dir = workspace / ".awm"
    awm_dir.mkdir()
    services_dir = awm_dir / "services"
    services_dir.mkdir()
    monkeypatch.setattr("awm.config.WORKSPACE_ROOT", workspace, raising=False)
    monkeypatch.setattr("awm.config.AWM_DIR", awm_dir, raising=False)
    monkeypatch.setattr("awm.config.SERVICES_DIR", services_dir, raising=False)
    monkeypatch.setattr("awm.persistence.databases.SERVICES_DIR", services_dir)

    from awm.persistence.databases import get_connection, init_service_db
    from awm.orchestrator import dao

    # Stand up a v5-shaped DB (no accept_spec / accept_budget) with old rows.
    init_service_db("orchestrator", _V5_SCHEMA, schema_version=5)
    conn = get_connection("orchestrator")
    conn.execute("INSERT INTO tasks (id, goal, state, created_at, updated_at) "
                 "VALUES ('t1', 'old task', 'blocked', 0, 0)")
    conn.execute("INSERT INTO contracts (id, name, spec, producer_task, "
                 "created_at) VALUES ('c1', 'old', 's', 't1', 0)")
    conn.commit()
    conn.close()

    # Migrate to v6 via the live dao.init (uses SCHEMA_VERSION=6 + MIGRATIONS).
    monkeypatch.setattr(dao, "_initialized", False)
    dao.init()

    conn = get_connection("orchestrator")
    try:
        ver = conn.execute("SELECT version FROM schema_version").fetchone()[0]
        assert ver == 6
        task_cols = {r["name"] for r in conn.execute("PRAGMA table_info(tasks)")}
        contract_cols = {r["name"]
                         for r in conn.execute("PRAGMA table_info(contracts)")}
        assert "accept_budget" in task_cols
        assert "accept_spec" in contract_cols

        # Old rows survive with the additive defaults (budget=2, spec NULL).
        t = conn.execute(
            "SELECT accept_budget FROM tasks WHERE id = 't1'").fetchone()
        assert t["accept_budget"] == 2
        c = conn.execute(
            "SELECT accept_spec FROM contracts WHERE id = 'c1'").fetchone()
        assert c["accept_spec"] is None
    finally:
        conn.close()
