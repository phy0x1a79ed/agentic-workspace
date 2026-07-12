"""Steering protocol (T1) — orchestrator side.

The durable ``attached`` flag is authoritative; the tasks table gains the
steering-handshake columns (``steer_user_done`` / ``steer_agent_ready`` /
``steer_requested``) and the ``objective`` record. This exercises:

* the v4→v5 migration (columns added, rows survive),
* the privileged ``set_steering`` mirror (patch-only, by task id AND by slug),
* ``orch_dag`` projecting the bits + objective *presence* (not the text),
* the ``orch_task_detail`` on-selection read (objective text + memories),
* ``orch_node_open`` seeding the new columns consistently.
"""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.smoke]


# ---------------------------------------------------------------------------
# Migration v4 -> v5
# ---------------------------------------------------------------------------


def _cols(conn, table: str) -> set[str]:
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}


class TestMigration:
    def test_v4_to_v5_adds_steering_columns_keeps_rows(self, tmp_path, monkeypatch):
        """A real v4 DB (title/tags/paused, version=4, a live row) migrates to v5:
        the row survives, gains the four steering columns at their defaults, and
        the version bumps."""
        import awm.persistence.databases as dbs_mod
        from awm.orchestrator.dao import (SCHEMA_SQL, MIGRATIONS, SCHEMA_VERSION,
                                          new_uuid)
        monkeypatch.setattr(dbs_mod, "SERVICES_DIR", tmp_path / "services")

        # A minimal v4 tasks table (the columns present as of v4).
        v4_sql = (
            "CREATE TABLE IF NOT EXISTS tasks ("
            "id TEXT PRIMARY KEY, goal TEXT NOT NULL DEFAULT '', "
            "title TEXT NOT NULL DEFAULT '', tags TEXT NOT NULL DEFAULT '[]', "
            "state TEXT NOT NULL DEFAULT 'blocked', "
            "is_root INTEGER NOT NULL DEFAULT 0, "
            "paused INTEGER NOT NULL DEFAULT 0, mode TEXT, workspace_slug TEXT, "
            "agent_ref TEXT, placement_token TEXT, plan_ref TEXT, "
            "attached INTEGER NOT NULL DEFAULT 0, "
            "replan_budget INTEGER NOT NULL DEFAULT 2, "
            "retry_count INTEGER NOT NULL DEFAULT 0, "
            "created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL);"
        )
        dbs_mod.init_service_db("orchestrator", v4_sql, schema_version=4)
        conn = dbs_mod.get_connection("orchestrator")
        tid = new_uuid()
        conn.execute(
            "INSERT INTO tasks (id, goal, state, created_at, updated_at) "
            "VALUES (?, ?, 'ready', 1, 1)", (tid, "legacy goal"))
        conn.commit()
        conn.close()

        dbs_mod.init_service_db("orchestrator", SCHEMA_SQL,
                                schema_version=SCHEMA_VERSION, migrations=MIGRATIONS)

        conn = dbs_mod.get_connection("orchestrator")
        cols = _cols(conn, "tasks")
        assert {"steer_user_done", "steer_agent_ready", "steer_requested",
                "objective"} <= cols
        row = conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
        assert row is not None and row["goal"] == "legacy goal"
        assert row["steer_user_done"] == 0 and row["steer_agent_ready"] == 0
        assert row["steer_requested"] == 0 and row["objective"] == ""
        ver = conn.execute("SELECT version FROM schema_version").fetchone()[0]
        assert ver == SCHEMA_VERSION
        conn.close()


# ---------------------------------------------------------------------------
# orch_node_open seeds the new columns consistently
# ---------------------------------------------------------------------------


def test_node_open_seeds_steering_defaults(orch):
    res = orch.operations.orch_node_open({"goal": ""})
    task = orch.DAO().get_task(res["task_id"])
    assert task["steer_user_done"] == 0
    assert task["steer_agent_ready"] == 0
    assert task["steer_requested"] == 0
    assert task["objective"] == ""
    # Born attached (drop-in), so the durable attach flag is the freeze signal.
    assert bool(task["attached"]) is True


# ---------------------------------------------------------------------------
# set_steering — the privileged patch-only mirror
# ---------------------------------------------------------------------------


class TestSetSteering:
    def test_patches_only_named_fields(self, orch):
        res = orch.operations.orch_node_open({"goal": "x"})
        tid = res["task_id"]
        agent_ref = orch.DAO().get_task(tid)["agent_ref"]

        # Write the objective + the agent's readiness bit; leave the rest.
        out = orch.operations.set_steering({
            "task_id": tid, "agent_ref": agent_ref,
            "objective": "# Objective\nship the thing", "steer_agent_ready": True})
        assert out["ok"] is True
        assert out["steer_agent_ready"] is True
        t = orch.DAO().get_task(tid)
        assert t["objective"] == "# Objective\nship the thing"
        assert t["steer_agent_ready"] == 1
        assert t["steer_user_done"] == 0        # untouched
        assert bool(t["attached"]) is True      # untouched (born attached)

    def test_resolves_by_workspace_slug(self, orch):
        res = orch.operations.orch_node_open({"goal": "x"})
        tid = res["task_id"]
        slug = res["workspace_slug"]
        orch.operations.set_steering({"workspace_slug": slug,
                                      "steer_requested": True})
        assert orch.DAO().get_task(tid)["steer_requested"] == 1

    def test_handshake_clear_is_a_full_patch(self, orch):
        res = orch.operations.orch_node_open({"goal": "x"})
        tid = res["task_id"]
        orch.operations.set_steering({
            "task_id": tid, "objective": "obj", "steer_agent_ready": True,
            "steer_user_done": True})
        # The agents side commits the detach by clearing attach + all bits.
        orch.operations.set_steering({
            "task_id": tid, "attached": False, "steer_user_done": False,
            "steer_agent_ready": False, "steer_requested": False})
        t = orch.DAO().get_task(tid)
        assert bool(t["attached"]) is False
        assert (t["steer_user_done"], t["steer_agent_ready"],
                t["steer_requested"]) == (0, 0, 0)
        assert t["objective"] == "obj"          # the record survives the detach

    def test_unknown_task_is_soft_error(self, orch):
        out = orch.operations.set_steering({"task_id": "nope"})
        assert out["ok"] is False


# ---------------------------------------------------------------------------
# Public projections — orch_dag + orch_task_detail
# ---------------------------------------------------------------------------


class TestProjections:
    def test_orch_dag_projects_bits_and_objective_presence(self, orch):
        res = orch.operations.orch_node_open({"goal": "x"})
        tid = res["task_id"]
        orch.operations.set_steering({
            "task_id": tid, "objective": "the record",
            "steer_agent_ready": True, "steer_requested": True})

        row = next(t for t in orch.operations.orch_dag({})["tasks"]
                   if t["task_id"] == tid)
        assert row["attached"] is True
        assert row["steer_agent_ready"] is True
        assert row["steer_requested"] is True
        assert row["steer_user_done"] is False
        # Presence only — the poll never carries the objective TEXT.
        assert row["has_objective"] is True
        assert "objective" not in row

    def test_orch_dag_objective_absent_is_false(self, orch):
        res = orch.operations.orch_node_open({"goal": "x"})
        row = next(t for t in orch.operations.orch_dag({})["tasks"]
                   if t["task_id"] == res["task_id"])
        assert row["has_objective"] is False

    def test_orch_status_projects_bits(self, orch):
        res = orch.operations.orch_node_open({"goal": "x"})
        tid = res["task_id"]
        orch.operations.set_steering({"task_id": tid, "steer_requested": True})
        srow = next(t for t in orch.operations.orch_status({})["tasks"]
                    if t["task_id"] == tid)
        assert srow["steer_requested"] is True
        assert srow["has_objective"] is False

    def test_orch_task_detail_returns_objective_and_memories(self, orch):
        res = orch.operations.orch_node_open({"goal": "ship"})
        tid = res["task_id"]
        orch.operations.set_steering({"task_id": tid, "objective": "the record"})

        detail = orch.operations.orch_task_detail({"task_id": tid})
        assert detail["ok"] is True
        assert detail["objective"] == "the record"
        # orch_node_open records an "attended-open" creation memory.
        kinds = [m["reason_type"] for m in detail["attempt_memories"]]
        assert "attended-open" in kinds
        # The bits ride along so a focus panel needs one read.
        assert detail["attached"] is True
        assert detail["steer_agent_ready"] is False

    def test_orch_task_detail_unknown_task(self, orch):
        assert orch.operations.orch_task_detail({"task_id": "nope"})["ok"] is False


# ---------------------------------------------------------------------------
# Terminal rests clear steering — a completed/failed/abandoned task is no
# longer a steering session (a stale ``attached`` on a terminal row would sit
# in the attention strip forever). The objective (ratified intent) is kept.
# ---------------------------------------------------------------------------


class TestTerminalClearsSteering:
    def _steered_task(self, orch, state="active"):
        dao = orch.DAO()
        tid = dao.create_task("g", state=state)
        dao.update_task(tid, attached=1, steer_user_done=1,
                        steer_agent_ready=1, steer_requested=1,
                        objective="keep me")
        return dao, tid

    def _assert_cleared(self, dao, tid):
        t = dao.get_task(tid)
        assert t["attached"] == 0
        assert (t["steer_user_done"], t["steer_agent_ready"],
                t["steer_requested"]) == (0, 0, 0)
        assert t["objective"] == "keep me"

    def test_complete_clears_steering(self, orch):
        dao, tid = self._steered_task(orch)
        assert orch.kernel.complete_task(dao, tid) is True
        assert dao.get_task(tid)["state"] == "completed"
        self._assert_cleared(dao, tid)

    def test_abandon_clears_steering(self, orch):
        dao, tid = self._steered_task(orch)
        orch.kernel.abandon(dao, tid)
        assert dao.get_task(tid)["state"] == "abandoned"
        self._assert_cleared(dao, tid)

    def test_failed_rest_clears_steering(self, orch):
        # Contract-unsatisfiable on an active leg rests ``failed``.
        dao, tid = self._steered_task(orch, state="active")
        orch.kernel.route_failure(dao, tid, "contract-unsatisfiable")
        assert dao.get_task(tid)["state"] == "failed"
        self._assert_cleared(dao, tid)

    def test_transient_retry_keeps_attached(self, orch):
        # A transient failure below the cap re-rests the SAME leg — the
        # steering session survives (the re-placement comes back attended).
        dao, tid = self._steered_task(orch, state="active")
        orch.kernel.route_failure(dao, tid, "transient-error")
        t = dao.get_task(tid)
        assert t["state"] == "plan_approved"
        assert t["attached"] == 1
