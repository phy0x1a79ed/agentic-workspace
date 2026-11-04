"""Schema migration to v5: kinds become tags, supersedes and tombstones become replies."""

from __future__ import annotations

import json
import sqlite3

import pytest

pytestmark = [pytest.mark.journal]


@pytest.fixture()
def old_db(tmp_path, monkeypatch):
    import awm.persistence.databases as pdbs
    from awm.journal import dao
    from tests.conftest import OLD_DDL
    monkeypatch.setattr(pdbs, "SERVICES_DIR", tmp_path)
    path = pdbs.service_db_path("journal")
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(OLD_DDL)
    rows = [
        ("j", "awm", "dev", "agent:awm/dev", "journal", "did it", {}, 1),
        ("m", "awm", "dev", "user:tony", "message", "hi", {}, 2),
        ("d", "awm", "dev", "agent:awm/dev", "debrief", "old style", {}, 3),
        ("g1", "", "workspace", "user:tony", "goal", "v1", {}, 4),
        ("g2", "", "workspace", "user:tony", "goal", "v2", {"supersedes": "g1"}, 5),
        ("g3", "", "workspace", "user:tony", "goal", "v3", {"supersedes": "g2"}, 6),
        ("t", "", "workspace", "user:tony", "goal", "obsolete", {"retires": "g3"}, 7),
        ("b1", "awm", "dev", "agent:awm/dev", "brief", "for x", {}, 8),
        ("b2", "awm", "dev", "agent:awm/dev", "brief", "for y", {"supersedes": "b1"}, 9),
    ]
    conn.executemany("INSERT INTO scope_posts VALUES (?,?,?,?,?,?,?,?)",
                     [(*r[:6], json.dumps(r[6]), r[7]) for r in rows])
    conn.commit()
    conn.close()
    monkeypatch.setattr(dao, "_initialized", False)
    dao.init()
    return path


def _tags(conn, pid):
    return {r[0] for r in conn.execute("SELECT tag_id FROM post_tags WHERE post_id=?", (pid,))}


def test_every_kind_becomes_a_tag(old_db):
    conn = sqlite3.connect(old_db)
    assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == 5
    assert "kind" not in [r[1] for r in conn.execute("PRAGMA table_info(scope_posts)")]
    assert _tags(conn, "j") == {"builtin:journal"} and _tags(conn, "m") == {"builtin:message"}
    assert _tags(conn, "d") == {"migrated:debrief"}
    desc = conn.execute("SELECT description FROM tags WHERE id='migrated:debrief'").fetchone()[0]
    assert "migrated" in desc


def test_supersedes_and_tombstones_become_replies(old_db):
    conn = sqlite3.connect(old_db)
    rows = {r[0]: r[1:] for r in conn.execute("SELECT id, reply_to, thread_id, meta FROM scope_posts")}
    assert rows["g2"][:2] == ("g1", "g1") and rows["g3"][:2] == ("g2", "g1")
    assert rows["t"][:2] == ("g3", "g1") and rows["b2"][:2] == ("b1", "b1")
    assert "supersedes" not in rows["g2"][2] and "retires" not in rows["t"][2]
    assert "builtin:superseded" in _tags(conn, "g1") and "builtin:superseded" in _tags(conn, "g2")
    assert "builtin:closed" in _tags(conn, "g3") and "builtin:superseded" in _tags(conn, "b1")
    assert _tags(conn, "t") == {"builtin:message"}


def test_the_migrated_goals_and_briefs_read_as_before(old_db):
    from awm.journal import briefs, goals
    assert goals.read_goals() == []
    assert [g.id for g in goals.history("g2")] == ["g1", "g2", "g3"]
    assert [r["id"] for r in goals.retirements(goals.history("g1"))] == ["t"]
    assert briefs.read("awm", "dev").id == "b2"
    assert [b.id for b in briefs.history("awm", "dev")] == ["b1", "b2"]
