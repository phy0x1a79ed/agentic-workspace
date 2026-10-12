"""Post-channel tests: live post/fetch/subscribe and the history render.

A scope IS the channel; these cover the surface that replaced the old
rooms/messages/session_logs trio.
"""
from __future__ import annotations

import pytest

pytestmark = [pytest.mark.journal]


# ---------------------------------------------------------------------------
# Live channel ops
# ---------------------------------------------------------------------------

class TestChannelOps:
    def test_post_and_fetch(self, journal_workspace):
        from awm.journal import channel
        channel.post("awm", "dev", author="user:alice", body="hello", kind="message")
        channel.post("awm", "dev", author="agent:awm/dev", body="hi back", kind="message")
        posts = channel.fetch(project="awm", scope="dev")
        assert [p.body for p in posts] == ["hello", "hi back"]  # oldest→newest
        assert posts[0].author == "user:alice"

    def test_order_desc_gives_last_n(self, journal_workspace):
        """order='desc' + limit pulls the most-recent N of a single channel,
        the 'last 5 session logs' path. Default stays oldest→newest."""
        from awm.journal import channel
        for i in range(5):
            channel.post("awm", "dev", author="agent:awm/dev",
                         body=f"entry {i}", kind="journal")
        # default single-channel order is oldest→newest (unchanged)
        asc = channel.fetch(project="awm", scope="dev", kind="journal")
        assert [p.body for p in asc] == [f"entry {i}" for i in range(5)]
        # order='desc' + limit → the last 2, newest-first
        last2 = channel.fetch(project="awm", scope="dev", kind="journal",
                              limit=2, order="desc")
        assert [p.body for p in last2] == ["entry 4", "entry 3"]

    def test_journal_kind_filter(self, journal_workspace):
        from awm.journal import channel
        channel.post("awm", "dev", author="agent:awm/dev", body="a message", kind="message")
        channel.post("awm", "dev", author="agent:awm/dev", body="debrief", kind="journal",
                     meta={"title": "Did it", "outcome": "success"})
        journals = channel.fetch(project="awm", scope="dev", kind="journal")
        assert len(journals) == 1
        assert journals[0].kind == "journal"
        assert journals[0].meta["outcome"] == "success"

    def test_search(self, journal_workspace):
        from awm.journal import channel, search_index
        channel.post("awm", "dev", author="user:alice", body="the quick brown fox", kind="message")
        channel.post("awm", "dev", author="user:alice", body="unrelated", kind="message")
        search_index.flush()
        hits = channel.fetch(project="awm", scope="dev", query="brown")
        assert hits[0].body == "the quick brown fox"
        assert hits[0].to_dict()["match"]["snippet"] == "the quick brown fox"

    def test_get_post(self, journal_workspace):
        from awm.journal import channel
        p = channel.post("awm", "dev", author="user:alice", body="findme", kind="message")
        got = channel.get_post(p.id)
        assert got is not None and got.body == "findme"

    def test_subscribe_unsubscribe(self, journal_workspace):
        from awm.journal import channel
        channel.subscribe("awm", "dev", "awm/helper")
        channel.subscribe("awm", "dev", "user:bob")
        subs = {(s.guest_kind, s.guest_ref) for s in channel.list_subscribers("awm", "dev")}
        assert ("agent", "awm/helper") in subs
        assert ("user", "user:bob") in subs
        assert channel.unsubscribe("awm", "dev", "awm/helper") is True
        assert channel.unsubscribe("awm", "dev", "awm/helper") is False
        subs2 = {(s.guest_kind, s.guest_ref) for s in channel.list_subscribers("awm", "dev")}
        assert ("agent", "awm/helper") not in subs2

    def test_non_literal_channel(self, journal_workspace):
        """Posting to a user/project inbox uses owner_project='' (non-literal)."""
        from awm.journal import channel
        channel.post("", "user:alice", author="agent:awm/dev", body="for alice", kind="message")
        posts = channel.fetch(project="", scope="user:alice")
        assert [p.body for p in posts] == ["for alice"]

    def test_post_fires_emitter(self, journal_workspace):
        """Every post fans out one cross-service `posts` emit (the live
        subscription consumers ride instead of a poll)."""
        from awm.journal import channel
        seen: list[dict] = []
        channel.set_emitter(lambda payload: seen.append(payload))
        try:
            channel.post("awm", "dev", author="user:alice",
                         body="ping", kind="message")
        finally:
            channel.set_emitter(None)
        assert len(seen) == 1
        ev = seen[0]
        assert ev["project"] == "awm"
        assert ev["scope"] == "dev"
        assert ev["post"]["body"] == "ping"
        assert ev["post"]["author"] == "user:alice"
        assert ev["post"]["kind"] == "message"

    def test_emitter_failure_does_not_break_post(self, journal_workspace):
        """A throwing emitter never fails the post (best-effort signalling)."""
        from awm.journal import channel

        def _boom(_payload):
            raise RuntimeError("emit down")

        channel.set_emitter(_boom)
        try:
            p = channel.post("awm", "dev", author="user:alice",
                             body="still saved", kind="message")
        finally:
            channel.set_emitter(None)
        assert channel.get_post(p.id) is not None


# ---------------------------------------------------------------------------
# journal_post input-shape: body must be the terminal parameter so a mangled
# tool-call serialization can't swallow trailing kind/meta params, and the
# history.md title fallback must neutralise a malformed/untitled body.
# ---------------------------------------------------------------------------

class TestScopePostInputShape:
    def test_body_is_last_param_in_manifest(self):
        from awm.journal.operations.posts import (
            POST_MANIFEST_FUNCTIONS,
        )
        fn = next(f for f in POST_MANIFEST_FUNCTIONS
                  if f["name"] == "post")
        names = [p["name"] for p in fn["params"]]
        assert names[-1] == "body", (
            f"body must be the terminal param to survive a serialization "
            f"bleed; got order {names}"
        )
        # structured params precede the free-text body
        for p in ("kind", "meta", "to_scope"):
            assert names.index(p) < names.index("body")

    def test_projected_tool_schema_lists_body_last(self):
        """The MCP inputSchema property order mirrors the manifest (catalog.py
        and operations.py both preserve declared order, no sorting)."""
        from awm.journal.operations.posts import (
            POST_MANIFEST_FUNCTIONS,
        )
        fn = next(f for f in POST_MANIFEST_FUNCTIONS
                  if f["name"] == "post")
        props = {}
        for p in fn["params"]:
            props[p["name"]] = {"type": p.get("type", "string")}
        assert list(props.keys())[-1] == "body"


class TestHistoryTitleNeutralise:
    def test_neutralise_strips_tags_and_newlines(self):
        from awm.journal.render import _neutralise_title
        raw = "BUG REPORT\nfoo bar\n</invoke>\n<meta>{...}</meta>"
        title = _neutralise_title(raw)
        assert "\n" not in title
        assert "</invoke>" not in title and "<meta>" not in title
        assert title.startswith("BUG REPORT foo bar")

    def test_neutralise_truncates_long_text(self):
        from awm.journal.render import _neutralise_title
        title = _neutralise_title("x" * 200)
        assert title.endswith("…") and len(title) == 81

    def test_untitled_journal_renders_clean_title(self, journal_workspace):
        """A kind=journal post with no meta.title must not render its raw
        multi-line / tag-bearing body as the history.md title."""
        from awm.journal import channel
        from awm.journal.render import generate_history_md
        channel.post(
            "awm", "dev", author="agent:awm/dev", kind="journal",
            meta={},  # no title — the swallowed-meta failure shape
            body="Did the thing\nacross lines\n</invoke>",
        )
        md = generate_history_md("awm", "dev", [])
        assert "</invoke>" not in md
        # the body-derived title is single-lined into the journal heading
        assert "Did the thing across lines" in md


class TestHistoryRender:
    """`.awm/history.md` is where the startup ritual sends every agent.

    Measured on awm/svc-scopes before this was fixed: a single project-wide
    ``ORDER BY ts DESC LIMIT`` ranked the scope's four journal entries 62nd,
    67th, 68th and 88th of 95, so the file rendered 20 entries, none of them
    the reader's own, and said nothing about the 75 it dropped.
    """

    def _journal(self, scope: str, title: str, ts=None):
        from awm.journal import channel
        channel.post("awm", scope, author=f"agent:awm/{scope}", kind="journal",
                     meta={"title": title}, body=title)

    def test_own_entries_survive_a_busy_project(self, journal_workspace):
        """A sibling flood must not push a scope out of its own history."""
        from awm.journal.render import generate_history_md
        self._journal("mine", "MY-OWN-ENTRY")
        for i in range(60):
            self._journal(f"sib{i}", f"sibling entry {i}")
        md = generate_history_md("awm", "mine", [])
        assert "MY-OWN-ENTRY" in md
        assert "This Scope's Journal" in md

    def test_states_how_many_it_omitted(self, journal_workspace):
        """Truncation must be visible: the file looked complete when it was not."""
        from awm.journal.render import generate_history_md
        for i in range(60):
            self._journal(f"sib{i}", f"sibling entry {i}")
        md = generate_history_md("awm", "mine", [])
        assert "not shown" in md

    def test_no_omitted_line_when_nothing_was_omitted(self, journal_workspace):
        """Absent when everything fits — the marker has to mean something."""
        from awm.journal.render import generate_history_md
        self._journal("mine", "only entry")
        md = generate_history_md("awm", "mine", [])
        assert "not shown" not in md

    def test_siblings_are_listed_as_given(self, journal_workspace):
        from awm.journal.render import generate_history_md
        md = generate_history_md("awm", "mine", ["sib-allocated", "sib-two"])
        assert "## Active Sibling Scopes" in md
        assert md.index("sib-allocated") < md.index("sib-two")

    def test_render_writes_the_scopes_history_file(self, journal_workspace):
        from awm.journal import render
        self._journal("mine", "WRITTEN-ENTRY")
        out = render.render("awm", "mine", [])
        path = journal_workspace["projects_dir"] / "awm" / "mine" / ".awm" / "history.md"
        assert out["path"] == str(path)
        assert path.read_text() == out["content"] and "WRITTEN-ENTRY" in out["content"]

    def test_render_without_write_leaves_the_file_alone(self, journal_workspace):
        from awm.journal import render
        out = render.render("awm", "mine", [], write=False)
        assert not (journal_workspace["projects_dir"] / "awm" / "mine").exists()
        assert out["written"] is False

    @pytest.mark.parametrize("project,scope", [("..", "x"), ("a/b", "x"), ("awm", "../x"), ("awm", "/x")])
    def test_render_refuses_a_path_escape(self, journal_workspace, project, scope):
        from awm.journal import render
        with pytest.raises(ValueError):
            render.render(project, scope, [])
