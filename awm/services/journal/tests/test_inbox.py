"""The inbox digest, the scope's read cursor, and fetch's refusal of a scope with no project."""
from __future__ import annotations

import itertools

import pytest

pytestmark = [pytest.mark.journal]


@pytest.fixture()
def clock(monkeypatch):
    """Posts one second apart, so order never rests on a same-millisecond tie."""
    from awm.journal import channel
    ticks = itertools.count(1_790_000_000_000, 1000)
    monkeypatch.setattr(channel, "now_ms", lambda: next(ticks))


def _post(body, *, scope="dev", project="awm", author="agent:metasmith/engine", meta=None):
    from awm.journal import channel
    return channel.post(project, scope, author=author, body=body, kind="message", meta=meta).id


class TestFetchNeedsProject:
    def test_scope_without_project_is_refused_and_names_the_projects(self, journal_workspace, clock):
        from awm.journal.operations.posts import _handle_fetch
        _post("for awm")
        _post("for metasmith", project="metasmith")
        with pytest.raises(ValueError) as err:
            _handle_fetch({"scope": "dev", "kind": "message"})
        assert "needs project" in str(err.value)
        assert "awm, metasmith" in str(err.value)
        assert "inbox" in str(err.value)

    def test_project_and_scope_read_only_that_channel(self, journal_workspace, clock):
        from awm.journal.operations.posts import _handle_fetch
        _post("for awm")
        _post("for metasmith", project="metasmith")
        out = _handle_fetch({"project": "awm", "scope": "dev", "kind": "message"})
        assert [p["body"] for p in out["posts"]] == ["for awm"]

    def test_non_literal_channel_needs_no_project(self, journal_workspace, clock):
        from awm.journal.operations.posts import _handle_fetch
        _post("hello alice", project="", scope="user:alice")
        out = _handle_fetch({"scope": "user:alice"})
        assert [p["body"] for p in out["posts"]] == ["hello alice"]

    def test_no_scope_still_searches_across_scopes(self, journal_workspace, clock):
        from awm.journal.operations.posts import _handle_fetch
        _post("one")
        _post("two", project="metasmith")
        assert _handle_fetch({"kind": "message"})["total"] == 2


class TestDigest:
    def test_newest_first_and_bounded(self, journal_workspace, clock):
        from awm.journal import inbox
        for i in range(60):
            _post(f"Post number {i}. " + "padding " * 500)
        out = inbox.digest("awm", "dev")
        assert len(out["posts"]) == inbox.DEFAULT_LIMIT
        assert out["posts"][0]["gist"] == "Post number 59."
        assert out["posts"][-1]["gist"] == f"Post number {60 - inbox.DEFAULT_LIMIT}."
        assert out["unread"] == out["total"] == 60
        assert out["more"] == 60 - inbox.DEFAULT_LIMIT
        assert out["ack_through"] == out["posts"][0]["id"]
        assert "body" not in out["posts"][0]
        assert len(inbox.digest("awm", "dev", limit=500)["posts"]) == inbox.MAX_LIMIT

    def test_size_does_not_grow_with_bodies(self, journal_workspace, clock):
        import json
        from awm.journal import inbox
        for i in range(inbox.MAX_LIMIT):
            _post("x" * 20_000, meta={"title": "t" * 1000})
        out = inbox.digest("awm", "dev", limit=inbox.MAX_LIMIT)
        assert all(len(p["gist"]) <= inbox.GIST_WIDTH for p in out["posts"])
        assert len(json.dumps(out)) < 20_000

    def test_only_its_own_channel(self, journal_workspace, clock):
        from awm.journal import inbox
        _post("mine")
        _post("not mine", project="metasmith")
        out = inbox.digest("awm", "dev")
        assert [p["gist"] for p in out["posts"]] == ["mine"]

    def test_gist_prefers_title_then_heading_then_first_sentence(self):
        from awm.journal.inbox import gist
        assert gist("body. more.", {"title": "The title"}) == "The title"
        assert gist("## Broken deploy\n\ndetails", {}) == "Broken deploy"
        assert gist("Hi, this is Claude on behalf of Tony. The gateway 502s. Details follow.",
                    {}) == "The gateway 502s."
        assert gist("<b>first</b>\nline. rest", {}) == "first line."

    def test_asks_flags_requests_not_reports(self):
        from awm.journal.inbox import asks
        assert asks("", {"type": "bug"}, "scope create is slow")
        assert asks("Can you look at the tether?", {}, "Can you look at the tether?")
        assert not asks("Fixed in e7ca6ce1 and deployed.", {}, "Fixed in e7ca6ce1 and deployed.")


class TestCursor:
    def test_acked_posts_drop_out_and_new_ones_appear(self, journal_workspace, clock):
        from awm.journal import inbox
        _post("first")
        _post("second")
        out = inbox.digest("awm", "dev")
        assert out["unread"] == 2
        late = _post("arrived after the digest")
        res = inbox.ack("awm", "dev", through=out["ack_through"], by="session-1")
        assert res["moved"] and res["unread"] == 1
        after = inbox.digest("awm", "dev")
        assert [p["id"] for p in after["posts"]] == [late]
        assert after["cursor"]["acked_by"] == "session-1"
        assert after["total"] == 3

    def test_all_lists_read_posts_marked(self, journal_workspace, clock):
        from awm.journal import inbox
        old = _post("old")
        inbox.ack("awm", "dev")
        new = _post("new")
        out = inbox.digest("awm", "dev", include_read=True)
        assert [(p["id"], p["read"]) for p in out["posts"]] == [(new, False), (old, True)]
        assert out["ack_through"] == new

    def test_own_posts_are_never_unread(self, journal_workspace, clock):
        from awm.journal import inbox
        _post("my own reply", author="agent:awm/dev")
        assert inbox.digest("awm", "dev")["unread"] == 0
        assert inbox.unread_count("awm", "dev") == 0

    def test_cursor_never_moves_back(self, journal_workspace, clock):
        from awm.journal import inbox
        first = _post("first")
        _post("second")
        inbox.ack("awm", "dev")
        res = inbox.ack("awm", "dev", through=first)
        assert not res["moved"] and res["unread"] == 0

    def test_cursor_is_per_channel(self, journal_workspace, clock):
        from awm.journal import inbox
        _post("awm")
        _post("metasmith", project="metasmith")
        inbox.ack("awm", "dev")
        assert inbox.unread_count("metasmith", "dev") == 1

    def test_ack_refuses_a_post_of_another_channel(self, journal_workspace, clock):
        from awm.journal import inbox
        other = _post("elsewhere", project="metasmith")
        with pytest.raises(ValueError):
            inbox.ack("awm", "dev", through=other)

    def test_history_names_the_unread_count(self, journal_workspace, clock):
        from awm.journal import inbox
        from awm.journal.render import generate_history_md
        assert "**Inbox:**" not in generate_history_md("awm", "dev")
        _post("one")
        _post("two")
        assert "**Inbox:** 2 unread messages" in generate_history_md("awm", "dev")
        inbox.ack("awm", "dev")
        assert "**Inbox:**" not in generate_history_md("awm", "dev")


def test_v1_database_gains_the_cursor_table(tmp_path, monkeypatch):
    import sqlite3
    import awm.persistence.databases as pdbs
    from awm.journal import dao
    services = tmp_path / "services"
    (services / "journal").mkdir(parents=True)
    monkeypatch.setattr(pdbs, "SERVICES_DIR", services)
    monkeypatch.setattr(dao, "_initialized", False)
    path = pdbs.service_db_path("journal")
    path.parent.mkdir(parents=True, exist_ok=True)
    v1 = dao.SCHEMA_SQL.replace(dao._CURSORS_SQL, "")
    pdbs.init_db_at(path, v1, schema_version=1)
    dao.init()
    conn = sqlite3.connect(path)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "scope_cursors" in tables
    assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == 2
