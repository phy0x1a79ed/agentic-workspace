"""The scopes schema migration."""
from __future__ import annotations

import sqlite3

import pytest

pytestmark = [pytest.mark.scopes]


class TestSchemaMigrationV3:
    """The v2→v3 migration drops the rooms-era ``agents.parent_id`` (and its
    dependent index) while preserving every agent row."""

    def test_drops_parent_id_and_preserves_rows(self, tmp_path):
        from awm.scopes.dao import MIGRATIONS

        db = sqlite3.connect(tmp_path / "v2.db")
        db.executescript(
            "CREATE TABLE agents (id TEXT PRIMARY KEY, project_id TEXT, "
            "scope TEXT, parent_id TEXT REFERENCES agents(id), status TEXT, "
            "agent_cli TEXT, branch TEXT, worktree TEXT, display_name TEXT, "
            "is_vagrant INT, created_at INT, retired_at INT);\n"
            "CREATE INDEX idx_agents_parent ON agents(parent_id);\n"
            "INSERT INTO agents VALUES "
            "('a1','p1','dev','par','active','claude','b','w','dev',0,1,NULL);\n"
        )
        db.commit()
        # Apply the real migration SQL the way the migration loop does (FKs off).
        db.executescript("PRAGMA foreign_keys=OFF;\n" + MIGRATIONS[(2, 3)])

        cols = [r[1] for r in db.execute("PRAGMA table_info(agents)").fetchall()]
        assert "parent_id" not in cols
        # Row + its surviving data preserved.
        assert db.execute(
            "SELECT id, scope, status FROM agents"
        ).fetchone() == ("a1", "dev", "active")
        # The dependent index is gone too.
        assert db.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND name='idx_agents_parent'"
        ).fetchone() is None
        db.close()
