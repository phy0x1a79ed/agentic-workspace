"""First-start copy of the post log from the scopes database."""

from __future__ import annotations

import sqlite3

import pytest

pytestmark = [pytest.mark.journal]

_SOURCE_DDL = """
CREATE TABLE scope_posts (id TEXT PRIMARY KEY, owner_project TEXT NOT NULL, owner_scope TEXT NOT NULL,
    author TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'message', body TEXT NOT NULL DEFAULT '',
    meta TEXT NOT NULL DEFAULT '{}', ts INTEGER NOT NULL);
CREATE TABLE scope_subscribers (owner_project TEXT NOT NULL, owner_scope TEXT NOT NULL,
    guest_kind TEXT NOT NULL, guest_ref TEXT NOT NULL, display_name TEXT NOT NULL,
    joined_at INTEGER NOT NULL, PRIMARY KEY (owner_project, owner_scope, guest_kind, guest_ref));
"""


@pytest.fixture()
def scopes_db(tmp_path):
    path = tmp_path / "scopes.db"
    db = sqlite3.connect(path)
    db.executescript(_SOURCE_DDL)
    db.executemany("INSERT INTO scope_posts VALUES (?,?,?,?,?,?,?,?)", [
        ("p1", "awm", "dev", "agent:awm/dev", "journal", "did it", '{"title": "T"}', 1000),
        ("p2", "", "workspace", "user:tony", "goal", "the aim", "{}", 2000),
        ("p3", "awm", "dev", "user:tony", "message", "hello", "{}", 3000),
    ])
    db.execute("INSERT INTO scope_subscribers VALUES ('awm','dev','user','user:bob','bob',5)")
    db.commit()
    db.close()
    return path


def _count(db_path, table):
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        conn.close()


def test_copies_posts_goals_and_subscribers(journal_workspace, scopes_db):
    from awm.journal import migrate
    from awm.persistence.databases import service_db_path
    out = migrate.migrate_from_scopes(source=scopes_db)
    target = service_db_path("journal")
    assert out["status"] == "migrated" and out["posts"] == 3 and out["subscribers"] == 1
    assert _count(target, "scope_posts") == 3 and _count(target, "scope_subscribers") == 1
    conn = sqlite3.connect(target)
    try:
        assert conn.execute("SELECT kind, ts FROM scope_posts WHERE id='p2'").fetchone() == ("goal", 2000)
    finally:
        conn.close()


def test_the_source_is_left_alone(journal_workspace, scopes_db):
    from awm.journal import migrate
    before = scopes_db.read_bytes()
    migrate.migrate_from_scopes(source=scopes_db)
    assert _count(scopes_db, "scope_posts") == 3
    assert scopes_db.read_bytes() == before


def test_a_rerun_adds_nothing(journal_workspace, scopes_db):
    from awm.journal import migrate
    from awm.persistence.databases import service_db_path
    migrate.migrate_from_scopes(source=scopes_db)
    again = migrate.migrate_from_scopes(source=scopes_db)
    assert again["status"] == "skipped"
    assert _count(service_db_path("journal"), "scope_posts") == 3


def test_a_log_with_posts_is_never_overwritten(journal_workspace, scopes_db):
    from awm.journal import channel, migrate
    from awm.persistence.databases import service_db_path
    channel.post("awm", "dev", author="user:a", body="written here first")
    assert migrate.migrate_from_scopes(source=scopes_db)["status"] == "skipped"
    assert _count(service_db_path("journal"), "scope_posts") == 1


def test_the_target_is_backed_up_before_the_copy(journal_workspace, scopes_db):
    from awm.journal import migrate
    from awm.persistence.databases import service_db_path
    out = migrate.migrate_from_scopes(source=scopes_db)
    backup = service_db_path("journal").parent / out["backup"].rsplit("/", 1)[1]
    assert backup.exists()
    assert _count(backup, "scope_posts") == 0


def test_no_scopes_database_is_a_no_op(journal_workspace, tmp_path):
    from awm.journal import migrate
    assert migrate.migrate_from_scopes(source=tmp_path / "absent.db")["status"] == "no-source"


def test_a_scopes_database_without_the_table_is_a_no_op(journal_workspace, tmp_path):
    from awm.journal import migrate
    path = tmp_path / "bare.db"
    sqlite3.connect(path).close()
    assert migrate.migrate_from_scopes(source=path)["status"] == "no-source"


def test_migrated_posts_are_searchable_after_a_reindex(journal_workspace, scopes_db):
    from awm.journal import channel, migrate, search_index
    migrate.migrate_from_scopes(source=scopes_db)
    search_index.run_reindex()
    posts, _, _ = channel.search(query="the aim", kind="goal")
    assert [p.id for p in posts] == ["p2"]
