"""Edits as versions, the trust bounds and the addendum sweep (fleet.md R107)."""

from __future__ import annotations

import sqlite3

import pytest

from awm.journal import bounds, channel, dao, versions
from awm.journal.operations.posts import POST_HANDLERS

pytestmark = [pytest.mark.unit]


@pytest.fixture()
def db(journal_workspace, monkeypatch):
    monkeypatch.setenv("AWM_NODE_NAME", "home")
    dao._initialized = False
    dao.init()
    return journal_workspace


def _post(body, scope="x", tags="journal"):
    return channel.post("awm", scope, author="agent:awm/" + scope, body=body, tags=tags)


def test_an_edit_keeps_every_version_and_reads_show_the_latest(db):
    p = _post("The deploy ran on alcor.\nAll green.")
    out = POST_HANDLERS["edit"]({"post_id": p.id, "old": "alcor", "new": "mizar", "reason": "wrong node"})
    assert out["version"] == 2
    assert channel.get_post(p.id).body == "The deploy ran on mizar.\nAll green."
    assert channel.get_post(p.id).to_dict()["version"] == 2
    POST_HANDLERS["edit"]({"post_id": p.id, "body": "Rewritten."})
    h = POST_HANDLERS["history"]({"post_id": p.id})["versions"]
    assert [v["version"] for v in h] == [1, 2, 3]
    assert h[0]["body"].startswith("The deploy ran on alcor")
    assert "-The deploy ran on alcor." in h[1]["diff"] and "+The deploy ran on mizar." in h[1]["diff"]
    assert h[1]["reason"] == "wrong node"


def test_an_unedited_post_is_version_1(db):
    p = _post("once")
    assert POST_HANDLERS["history"]({"post_id": p.id})["versions"][0]["version"] == 1
    assert "version" not in p.to_dict()


@pytest.mark.parametrize("bad", [{"old": "nope", "new": "x"}, {"body": "same"}, {}])
def test_a_bad_edit_is_refused(db, bad):
    p = _post("same")
    with pytest.raises(ValueError):
        POST_HANDLERS["edit"]({"post_id": p.id, **bad})


#: scope_posts as it stood at schema v3, before versions (3,4) and tags (4,5).
_V3_POSTS = """\
CREATE TABLE scope_posts (
    id TEXT PRIMARY KEY, owner_project TEXT NOT NULL, owner_scope TEXT NOT NULL,
    author TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'message', body TEXT NOT NULL DEFAULT '',
    meta TEXT NOT NULL DEFAULT '{}', ts INTEGER NOT NULL
);
CREATE INDEX idx_scope_posts_kind ON scope_posts(owner_project, owner_scope, kind, ts);
"""


def test_a_v3_database_reaches_v5_with_every_post_at_version_1(tmp_path, monkeypatch):
    import awm.persistence.databases as pdbs
    services = tmp_path / "services"
    (services / "journal").mkdir(parents=True)
    monkeypatch.setattr(pdbs, "SERVICES_DIR", services)
    monkeypatch.setattr(dao, "_initialized", False)
    path = pdbs.service_db_path("journal")
    pdbs.init_db_at(path, _V3_POSTS + dao._CURSORS_SQL + dao.READS_SQL, schema_version=3)
    con = sqlite3.connect(path)
    con.execute("INSERT INTO scope_posts (id, owner_project, owner_scope, author, kind, body, ts) "
                "VALUES ('p1','awm','x','system','journal','old',1)")
    con.commit()
    con.close()
    dao.init()
    con = sqlite3.connect(path)
    assert con.execute("SELECT version FROM schema_version").fetchone() == (dao.SCHEMA_VERSION,)
    assert con.execute("SELECT version FROM scope_posts WHERE id='p1'").fetchone() == (1,)
    assert con.execute("SELECT count(*) FROM post_versions").fetchone() == (0,)
    assert con.execute("SELECT tag_id FROM post_tags WHERE post_id='p1'").fetchall() == [("builtin:journal",)]
    con.close()


def test_untrusted_reads_and_writes_only_its_own_scope(db):
    mine, other = _post("mine", "x"), _post("other", "y")
    who = {"_caller_trust": "untrusted", "_caller_scope": "awm/x"}
    got = POST_HANDLERS["fetch"]({**who, "project": "awm"})["posts"]
    assert [p["id"] for p in got] == [mine.id]
    with pytest.raises(bounds.Refused):
        POST_HANDLERS["fetch"]({**who, "project": "awm", "scope": "y"})
    assert POST_HANDLERS["fetch"]({**who, "post_id": other.id})["posts"] == []
    with pytest.raises(bounds.Refused):
        POST_HANDLERS["edit"]({**who, "post_id": other.id, "body": "mine now"})


def test_cooperative_reads_its_node_and_writes_only_its_scope(db):
    _post("a", "x")
    other = _post("b", "y")
    who = {"_caller_trust": "cooperative", "_caller_scope": "awm/x"}
    assert len(POST_HANDLERS["fetch"]({**who, "project": "awm"})["posts"]) == 2
    with pytest.raises(bounds.Refused):
        POST_HANDLERS["post"]({**who, "project": "awm", "scope": "y", "author": "a", "body": "b"})
    with pytest.raises(bounds.Refused):
        POST_HANDLERS["edit"]({**who, "post_id": other.id, "body": "c"})
    POST_HANDLERS["post"]({**who, "project": "awm", "scope": "x", "author": "a", "body": "b"})


def test_cooperative_from_another_node_reads_nothing_here(db):
    _post("a", "x")
    with pytest.raises(bounds.Refused):
        POST_HANDLERS["fetch"]({"_caller_trust": "cooperative", "_caller_scope": "awm/x", "project": "awm"},
                               as_="peer:work")


def test_a_level_without_a_scope_is_refused(db):
    with pytest.raises(bounds.Refused):
        POST_HANDLERS["fetch"]({"_caller_trust": "cooperative"})


def test_the_sweep_folds_addenda_into_their_targets(db):
    target = _post("Debrief: shipped v2.")
    add = _post(f"Addendum to the debrief ({target.id[:8]}). The deploy also needs a restart.")
    unrelated = _post("Addendum to nothing in particular.")
    listed = POST_HANDLERS["addenda"]({"project": "awm"})
    assert listed["addenda"] == [{"addendum": add.id, "target": target.id, "project": "awm", "scope": "x",
                                  "first_line": add.body.splitlines()[0][:160]}]
    assert channel.get_post(target.id).body == "Debrief: shipped v2."
    POST_HANDLERS["addenda"]({"project": "awm", "apply": True})
    folded = channel.get_post(target.id)
    assert "needs a restart" in folded.body and folded.version == 2
    ids = [p["id"] for p in POST_HANDLERS["fetch"]({"project": "awm", "scope": "x"})["posts"]]
    assert add.id not in ids and unrelated.id in ids
    assert channel.get_post(add.id).meta["folded_into"] == target.id
    assert "folded" in channel.get_post(add.id).tags
    named = POST_HANDLERS["fetch"]({"project": "awm", "scope": "x", "tags": "folded"})["posts"]
    assert [p["id"] for p in named] == [add.id]


def test_only_trusted_sweeps(db):
    with pytest.raises(bounds.Refused):
        POST_HANDLERS["addenda"]({"_caller_trust": "cooperative", "_caller_scope": "awm/x"})
