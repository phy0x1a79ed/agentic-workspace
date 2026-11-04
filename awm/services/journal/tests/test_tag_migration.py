"""Schema migration to v5: kinds become tags, goals become journals, cites become citations."""

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
        ("x1", "", "project:_goaltest", "user:tony", "goal", "probe", {}, 10),
        ("x2", "awm", "dev", "user:tony", "goal", "TEST-goal", {}, 11),
        ("x3", "awm", "dev", "user:tony", "goal", "test cleanup", {}, 12),
        ("c", "awm", "dev", "agent:awm/dev", "journal", "built on j",
         {"cites": [{"journal": "j", "reason": "the base"}, {"journal": "gone"}]}, 13),
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


def test_goals_become_journals_and_the_test_rows_go(old_db):
    conn = sqlite3.connect(old_db)
    ids = {r[0] for r in conn.execute("SELECT id FROM scope_posts")}
    assert not {"x1", "x2", "x3"} & ids
    for g in ("g1", "g2", "g3", "t"):
        assert _tags(conn, g) == {"builtin:journal"}
    meta = json.loads(conn.execute("SELECT meta FROM scope_posts WHERE id='g2'").fetchone()[0])
    assert meta == {"supersedes": "g1"}
    assert conn.execute("SELECT reply_to FROM scope_posts WHERE id='g2'").fetchone()[0] is None
    assert conn.execute("SELECT 1 FROM tags WHERE name='goal'").fetchone() is None


def test_a_brief_supersedes_by_reply(old_db):
    from awm.journal import briefs
    conn = sqlite3.connect(old_db)
    assert conn.execute("SELECT reply_to, thread_id FROM scope_posts WHERE id='b2'").fetchone() == (
        "b1", "b1")
    assert "builtin:superseded" in _tags(conn, "b1")
    assert briefs.read("awm", "dev").id == "b2"
    assert [b.id for b in briefs.history("awm", "dev")] == ["b1", "b2"]


def test_meta_cites_become_citations_and_the_reasons_stay(old_db):
    from awm.journal import channel
    conn = sqlite3.connect(old_db)
    assert conn.execute("SELECT cited_id FROM post_cites WHERE post_id='c'").fetchall() == [("j",)]
    post = channel.get_post("c")
    assert post.to_dict()["reply_to"] == ["j"]
    assert post.meta == {"cite_reasons": {"j": "the base"}}
