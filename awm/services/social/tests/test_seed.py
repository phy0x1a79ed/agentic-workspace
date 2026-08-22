"""Tests for the discord → social operator seed migration."""

from __future__ import annotations

import sqlite3

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.smoke]


def _make_discord_db(path, rows):
    """Build a minimal legacy discord.db with a discord_operators table."""
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE discord_operators ("
        "discord_user_id TEXT PRIMARY KEY, awm_user TEXT, added_at TEXT)"
    )
    conn.executemany(
        "INSERT INTO discord_operators VALUES (?, ?, ?)", rows)
    conn.commit()
    conn.close()


class TestSeed:
    def test_migrates_rows_scoped_to_discord(self, awm_workspace, tmp_path):
        from awm.social import seed
        from awm.social import operators as ops

        src = tmp_path / "discord.db"
        _make_discord_db(src, [("111", "tony", "2025-01-01"),
                               ("222", "alice", "2025-01-02")])

        n = seed.seed_from_discord_db(src)
        assert n == 2
        assert ops.lookup("discord", "111") == "tony"
        assert ops.lookup("discord", "222") == "alice"
        rows = ops.list_operators("discord")
        assert {r["platform_user_id"] for r in rows} == {"111", "222"}

    def test_idempotent(self, awm_workspace, tmp_path):
        from awm.social import seed
        from awm.social import operators as ops

        src = tmp_path / "discord.db"
        _make_discord_db(src, [("111", "tony", "2025-01-01")])
        assert seed.seed_from_discord_db(src) == 1
        assert seed.seed_from_discord_db(src) == 1  # re-run, still one row
        assert len(ops.list_operators("discord")) == 1

    def test_missing_file_is_noop(self, awm_workspace, tmp_path):
        from awm.social import seed
        assert seed.seed_from_discord_db(tmp_path / "absent.db") == 0

    def test_missing_table_is_noop(self, awm_workspace, tmp_path):
        from awm.social import seed
        empty = tmp_path / "empty.db"
        sqlite3.connect(empty).close()
        assert seed.seed_from_discord_db(empty) == 0
