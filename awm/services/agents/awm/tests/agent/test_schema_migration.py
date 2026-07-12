"""agent_instances v3 schema — fresh shape, project dropped, index uniqueness,
migration paths (v1→v3 chain + v2→v3 DROP COLUMN)."""

from __future__ import annotations

import sqlite3

import pytest

pytestmark = [pytest.mark.agent, pytest.mark.smoke]

from awm.agents.dao import AgentsDAO
from awm.agents._time import now_ms

_PLACEMENT_COLS = {"mode", "task_ref", "agent_ref", "placement_token"}


def _cols(conn_or_dao, table: str) -> set[str]:
    if isinstance(conn_or_dao, AgentsDAO):
        return {r["name"]
                for r in conn_or_dao.query_all(f"PRAGMA table_info({table})")}
    return {r["name"] for r in conn_or_dao.execute(f"PRAGMA table_info({table})")}


class TestFreshSchema:
    def test_placement_columns_present(self, agents_env):
        dao = AgentsDAO()
        assert _PLACEMENT_COLS <= _cols(dao, "agent_instances")

    def test_project_column_dropped(self, agents_env):
        dao = AgentsDAO()
        assert "project" not in _cols(dao, "agent_instances")
        assert "project" not in _cols(dao, "agent_transcript")
        # scope is the natural key on both tables now.
        assert "scope" in _cols(dao, "agent_instances")
        assert "scope" in _cols(dao, "agent_transcript")

    def test_mode_defaults_to_conversational(self, agents_env):
        dao = AgentsDAO()
        iid = dao.open_instance(scope="s", log_path=None,
                                cli_session_id=None, started_at=now_ms())
        assert dao.get_instance(iid)["mode"] == "conversational"

    def test_placement_token_unique(self, agents_env):
        dao = AgentsDAO()
        dao.open_task_instance(
            scope="s1", log_path=None, cli_session_id=None,
            started_at=now_ms(), mode="worker", task_ref="T", agent_ref="agt-1",
            placement_token="plt-dup")
        with pytest.raises(sqlite3.IntegrityError):
            dao.open_task_instance(
                scope="s2", log_path=None, cli_session_id=None,
                started_at=now_ms(), mode="worker", task_ref="T2",
                agent_ref="agt-2", placement_token="plt-dup")

    def test_null_placement_tokens_coexist(self, agents_env):
        # The unique index is partial (WHERE placement_token IS NOT NULL), so
        # every row with a NULL token coexists fine.
        dao = AgentsDAO()
        dao.open_instance(scope="a", log_path=None,
                          cli_session_id=None, started_at=now_ms())
        dao.open_instance(scope="b", log_path=None,
                          cli_session_id=None, started_at=now_ms())  # no raise


class TestMigration:
    def test_v1_to_v3_chain(self, tmp_path, monkeypatch):
        """A real v1 DB (old DDL, version=1, a live row with project) migrates
        the whole chain to v3: the row survives, gains the placement columns and
        mode='conversational', and the ``project`` column is gone."""
        import awm.persistence.databases as dbs_mod
        from awm.agents.dao import SCHEMA_SQL, MIGRATIONS, SCHEMA_VERSION
        monkeypatch.setattr(dbs_mod, "SERVICES_DIR", tmp_path / "services")

        v1_sql = (
            "CREATE TABLE IF NOT EXISTS agent_instances ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, project TEXT NOT NULL, "
            "scope TEXT NOT NULL, cli_session_id TEXT, log_path TEXT, "
            "started_at INTEGER NOT NULL, ended_at INTEGER, "
            "data TEXT NOT NULL DEFAULT '{}');\n"
            "CREATE TABLE IF NOT EXISTS agent_transcript ("
            "id TEXT PRIMARY KEY, project TEXT NOT NULL, scope TEXT NOT NULL, "
            "kind TEXT NOT NULL, body TEXT NOT NULL DEFAULT '', "
            "meta TEXT NOT NULL DEFAULT '{}', ts INTEGER NOT NULL);"
        )
        dbs_mod.init_service_db("agents", v1_sql, schema_version=1)
        conn = dbs_mod.get_connection("agents")
        conn.execute(
            "INSERT INTO agent_instances "
            "(project, scope, started_at, data) VALUES (?,?,?,?)",
            ("p", "legacy", now_ms(), "{}"))
        conn.commit()
        conn.close()

        dbs_mod.init_service_db("agents", SCHEMA_SQL, schema_version=SCHEMA_VERSION,
                                migrations=MIGRATIONS)

        conn = dbs_mod.get_connection("agents")
        cols = _cols(conn, "agent_instances")
        assert _PLACEMENT_COLS <= cols
        assert "project" not in cols
        assert "project" not in _cols(conn, "agent_transcript")
        row = conn.execute(
            "SELECT * FROM agent_instances WHERE scope='legacy'").fetchone()
        assert row is not None
        assert row["mode"] == "conversational"
        ver = conn.execute("SELECT version FROM schema_version").fetchone()[0]
        assert ver == 3
        conn.close()

    def test_v2_to_v3_drops_project_keeps_rows(self, tmp_path, monkeypatch):
        """A v2 DB (project column + the placement columns) migrates to v3: the
        ``project`` column is DROPPED from both tables and rows survive."""
        import awm.persistence.databases as dbs_mod
        from awm.agents.dao import SCHEMA_SQL, MIGRATIONS, SCHEMA_VERSION
        monkeypatch.setattr(dbs_mod, "SERVICES_DIR", tmp_path / "services")

        v2_sql = (
            "CREATE TABLE IF NOT EXISTS agent_instances ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, project TEXT NOT NULL, "
            "scope TEXT NOT NULL, cli_session_id TEXT, log_path TEXT, "
            "started_at INTEGER NOT NULL, ended_at INTEGER, "
            "data TEXT NOT NULL DEFAULT '{}', "
            "mode TEXT NOT NULL DEFAULT 'conversational', task_ref TEXT, "
            "agent_ref TEXT, placement_token TEXT);\n"
            "CREATE INDEX IF NOT EXISTS idx_agent_instances_scope_started "
            "ON agent_instances(project, scope, started_at DESC);\n"
            "CREATE INDEX IF NOT EXISTS idx_agent_instances_open "
            "ON agent_instances(project, scope) WHERE ended_at IS NULL;\n"
            "CREATE TABLE IF NOT EXISTS agent_transcript ("
            "id TEXT PRIMARY KEY, project TEXT NOT NULL, scope TEXT NOT NULL, "
            "kind TEXT NOT NULL, body TEXT NOT NULL DEFAULT '', "
            "meta TEXT NOT NULL DEFAULT '{}', ts INTEGER NOT NULL);\n"
            "CREATE INDEX IF NOT EXISTS idx_agent_transcript_scope_ts "
            "ON agent_transcript(project, scope, ts);"
        )
        dbs_mod.init_service_db("agents", v2_sql, schema_version=2)
        conn = dbs_mod.get_connection("agents")
        conn.execute(
            "INSERT INTO agent_instances "
            "(project, scope, started_at, data, mode) VALUES (?,?,?,?,?)",
            ("p", "v2row", now_ms(), "{}", "worker"))
        conn.execute(
            "INSERT INTO agent_transcript (id, project, scope, kind, body, meta, ts) "
            "VALUES (?,?,?,?,?,?,?)",
            ("tid", "p", "v2row", "message", "hi", "{}", now_ms()))
        conn.commit()
        conn.close()

        dbs_mod.init_service_db("agents", SCHEMA_SQL, schema_version=SCHEMA_VERSION,
                                migrations=MIGRATIONS)

        conn = dbs_mod.get_connection("agents")
        assert "project" not in _cols(conn, "agent_instances")
        assert "project" not in _cols(conn, "agent_transcript")
        irow = conn.execute(
            "SELECT * FROM agent_instances WHERE scope='v2row'").fetchone()
        assert irow is not None and irow["mode"] == "worker"
        trow = conn.execute(
            "SELECT * FROM agent_transcript WHERE scope='v2row'").fetchone()
        assert trow is not None and trow["body"] == "hi"
        ver = conn.execute("SELECT version FROM schema_version").fetchone()[0]
        assert ver == 3
        conn.close()
