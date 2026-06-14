"""T1: agent_instances v2 schema — fresh shape, index uniqueness, v1→v2 path."""

from __future__ import annotations

import sqlite3

import pytest

pytestmark = [pytest.mark.agent, pytest.mark.smoke]

from awm.agents.dao import AgentsDAO
from awm.agents._time import now_ms

_V2_COLS = {"mode", "task_ref", "agent_ref", "parent_agent_ref", "placement_token"}


class TestFreshSchema:
    def test_v2_columns_present(self, agents_env):
        dao = AgentsDAO()
        cols = {r["name"]
                for r in dao.query_all("PRAGMA table_info(agent_instances)")}
        assert _V2_COLS <= cols

    def test_mode_defaults_to_conversational(self, agents_env):
        dao = AgentsDAO()
        iid = dao.open_instance(project="p", scope="s", log_path=None,
                                cli_session_id=None, started_at=now_ms())
        assert dao.get_instance(iid)["mode"] == "conversational"

    def test_placement_token_unique(self, agents_env):
        dao = AgentsDAO()
        dao.open_task_instance(
            project="p", scope="s1", log_path=None, cli_session_id=None,
            started_at=now_ms(), mode="worker", task_ref="T", agent_ref="agt-1",
            parent_agent_ref=None, placement_token="plt-dup")
        with pytest.raises(sqlite3.IntegrityError):
            dao.open_task_instance(
                project="p", scope="s2", log_path=None, cli_session_id=None,
                started_at=now_ms(), mode="worker", task_ref="T2",
                agent_ref="agt-2", parent_agent_ref=None,
                placement_token="plt-dup")

    def test_null_placement_tokens_coexist(self, agents_env):
        # The unique index is partial (WHERE placement_token IS NOT NULL), so
        # every conversational row (NULL token) coexists fine.
        dao = AgentsDAO()
        dao.open_instance(project="p", scope="a", log_path=None,
                          cli_session_id=None, started_at=now_ms())
        dao.open_instance(project="p", scope="b", log_path=None,
                          cli_session_id=None, started_at=now_ms())  # no raise


class TestV1ToV2Migration:
    def test_migrate_preserves_rows_and_adds_columns(self, tmp_path, monkeypatch):
        """A real v1 DB (old DDL, version=1, a live row) migrates to v2: the row
        survives, defaults to mode='conversational', and the new columns exist."""
        import awm.persistence.databases as dbs_mod
        from awm.agents.dao import SCHEMA_SQL, MIGRATIONS
        monkeypatch.setattr(dbs_mod, "SERVICES_DIR", tmp_path / "services")

        v1_sql = (
            "CREATE TABLE IF NOT EXISTS agent_instances ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, project TEXT NOT NULL, "
            "scope TEXT NOT NULL, cli_session_id TEXT, log_path TEXT, "
            "started_at INTEGER NOT NULL, ended_at INTEGER, "
            "data TEXT NOT NULL DEFAULT '{}');"
        )
        # Boot a v1 DB and seed one row.
        dbs_mod.init_service_db("agents", v1_sql, schema_version=1)
        conn = dbs_mod.get_connection("agents")
        conn.execute(
            "INSERT INTO agent_instances "
            "(project, scope, started_at, data) VALUES (?,?,?,?)",
            ("p", "legacy", now_ms(), "{}"))
        conn.commit()
        conn.close()

        # Migrate to v2 with the real SCHEMA_SQL + MIGRATIONS.
        dbs_mod.init_service_db("agents", SCHEMA_SQL, schema_version=2,
                                migrations=MIGRATIONS)

        conn = dbs_mod.get_connection("agents")
        cols = {r["name"]
                for r in conn.execute("PRAGMA table_info(agent_instances)")}
        assert _V2_COLS <= cols
        row = conn.execute(
            "SELECT * FROM agent_instances WHERE scope='legacy'").fetchone()
        assert row is not None
        assert row["mode"] == "conversational"
        ver = conn.execute("SELECT version FROM schema_version").fetchone()[0]
        assert ver == 2
        conn.close()
