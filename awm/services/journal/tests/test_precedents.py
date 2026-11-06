"""The one-shot import of precedence.db as posts tagged precedent (fleet.md R107 item 9)."""

from __future__ import annotations

import sqlite3

import pytest

pytestmark = [pytest.mark.journal]

# precedence.db as the precedence service wrote it.
_DDL = """
CREATE TABLE decisions (id TEXT PRIMARY KEY, context TEXT NOT NULL, question TEXT NOT NULL,
    decision TEXT NOT NULL, created TEXT, added TEXT, status TEXT NOT NULL DEFAULT 'active',
    superseded_by TEXT, source TEXT, source_ref TEXT, content_hash TEXT, embedded_hash TEXT,
    upvotes INTEGER NOT NULL DEFAULT 0, downvotes INTEGER NOT NULL DEFAULT 0,
    seen_count INTEGER NOT NULL DEFAULT 0, last_seen TEXT);
CREATE TABLE notes (id TEXT PRIMARY KEY, decision_id TEXT NOT NULL, body TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'comment', author TEXT, created TEXT);
CREATE TABLE tags (decision_id TEXT NOT NULL, tag TEXT NOT NULL, PRIMARY KEY (decision_id, tag));
"""


@pytest.fixture()
def source(tmp_path):
    path = tmp_path / "precedence.db"
    db = sqlite3.connect(path)
    db.executescript(_DDL)
    db.executemany(
        "INSERT INTO decisions (id, context, question, decision, created, status, superseded_by,"
        " upvotes, seen_count) VALUES (?,?,?,?,?,?,?,?,?)", [
            ("old", "PRs", "Open a PR?", "Yes, always.", "2026-01-01", "superseded", "new", 3, 9),
            ("new", "PRs", "Open a PR?", "No, land it yourself.", "2026-06-01", "active", None, 1, 2),
            ("gone", "Fonts", "Serif?", "Serif.", "2026-02-01T10:00:00+00:00", "retired", None, 0, 0),
        ])
    db.executemany("INSERT INTO tags VALUES (?,?)",
                   [("new", "git"), ("new", "Release Flow"), ("old", "git"), ("gone", "avatar")])
    db.executemany("INSERT INTO notes VALUES (?,?,?,?,?,?)", [
        ("n1", "new", "Holds on altair too.", "amendment", "agent", "2026-06-02"),
        ("n2", "gone", "Nobody asked again.", "comment", None, "2026-03-01"),
    ])
    db.commit()
    db.close()
    return path


def test_decisions_become_precedent_posts_with_their_threads(journal_workspace, source):
    from awm.journal import channel, precedents, tags
    out = precedents.import_db(source)
    assert out["decisions"] == 3 and out["tags_added"] == ["avatar", "git", "release-flow"]
    old = channel.get_post(precedents.post_id("decision", "old"))
    new = channel.get_post(precedents.post_id("decision", "new"))
    gone = channel.get_post(precedents.post_id("decision", "gone"))
    assert (old.project, old.scope) == ("", "workspace")
    assert new.body == "Context: PRs\n\nQuestion: Open a PR?\n\nDecision: No, land it yourself."
    assert set(old.tags) == {"precedent", "git", "superseded"}
    assert set(new.tags) == {"precedent", "git", "release-flow"}
    assert new.reply_to == old.id and new.thread_id == old.id
    assert "closed" in gone.tags
    assert "upvotes" not in new.meta and "seen_count" not in new.meta
    [note] = [c for c in channel.thread_posts(old.id) if c.reply_to == new.id]
    assert note.body == "Amendment: Holds on altair too." and note.author == "user:agent"
    assert tags.by_name("git").description.startswith("A topic tag")


def test_a_rerun_adds_nothing(journal_workspace, source):
    from awm.journal import precedents
    from awm.journal.dao import JournalDAO
    precedents.import_db(source)
    n = JournalDAO().query_one("SELECT COUNT(*) AS n FROM scope_posts")["n"]
    again = precedents.import_db(source)
    assert again["already"] == 3 and again["decisions"] == 0
    assert JournalDAO().query_one("SELECT COUNT(*) AS n FROM scope_posts")["n"] == n


def test_a_dry_run_writes_nothing(journal_workspace, source):
    from awm.journal import precedents
    from awm.journal.dao import JournalDAO
    assert precedents.import_db(source, dry_run=True)["decisions"] == 3
    assert JournalDAO().query_one("SELECT COUNT(*) AS n FROM scope_posts")["n"] == 0


def test_precedents_reach_search_as_their_own_kind(journal_workspace, source):
    from awm.journal import precedents
    from awm.journal.feed import changes
    precedents.import_db(source)
    docs = {c["id"]: c["entry"] for c in changes(None, 1000)["changes"] if "entry" in c}
    root = docs[precedents.post_id("decision", "old")]
    assert "precedent" in root["tag"] and "land it yourself" in root["text"]
    assert precedents.post_id("decision", "new") not in docs


def test_a_precedent_tagged_with_a_marked_project_is_hidden_from_peers(journal_workspace, source):
    from awm.config import privacy
    from awm.journal import precedents, private
    from awm.journal.feed import changes
    precedents.import_db(source)
    privacy.write_marks([{"project": "avatar", "scope": None,
                          "path": str(journal_workspace["projects_dir"] / "avatar")}])
    ids = {c["id"] for c in changes(None, 1000, private.exclusion("peer"))["changes"] if "entry" in c}
    assert precedents.post_id("decision", "gone") not in ids
    assert precedents.post_id("decision", "old") in ids
