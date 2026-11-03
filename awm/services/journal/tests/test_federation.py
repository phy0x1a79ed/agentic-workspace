"""Federation surface of the journal: edge-stamped origins, peer reads, verb effects."""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.journal]


# ---------------------------------------------------------------------------
# Origin comes from the edge identity, never from the caller's `author`
# ---------------------------------------------------------------------------

def _post(as_, author="user:mallory", body="hello", kind="message"):
    from awm.journal.operations.posts import _handle_post
    args = {"project": "awm", "scope": "dev", "author": author, "body": body, "kind": kind}
    return _handle_post(args, as_)["post"]


@pytest.mark.parametrize("as_", ["peer:capella", "peer:shaula", "peer"])
def test_peer_identity_replaces_the_callers_author(journal_workspace, as_):
    post = _post(as_, author="agent:awm/dev")
    assert post["author"] == as_


def test_peer_origin_applies_to_journals_and_messages(journal_workspace):
    assert _post("peer:capella", kind="journal", body="debrief")["author"] == "peer:capella"
    assert _post("peer:capella", kind="message", body="ping")["author"] == "peer:capella"


def test_peer_origin_survives_a_refetch_and_an_author_filter(journal_workspace):
    from awm.journal import channel
    post = _post("peer:capella")
    assert channel.get_post(post["id"]).author == "peer:capella"
    found = channel.fetch(project="awm", scope="dev", author="peer:capella")
    assert [p.id for p in found] == [post["id"]]


@pytest.mark.parametrize("as_", [None, "tony", "agent:awm/dev", "user:tony"])
def test_other_callers_keep_the_author_they_sent(journal_workspace, as_):
    assert _post(as_, author="agent:awm/dev")["author"] == "agent:awm/dev"


def test_a_local_caller_cannot_claim_a_peer_author(journal_workspace):
    """Only the edge stamp can mint `peer:`; an unstamped caller sending one is refused."""
    from awm.journal import channel
    with pytest.raises(ValueError, match="reserved"):
        _post(None, author="peer:capella")
    with pytest.raises(ValueError, match="reserved"):
        _post("tony", author="peer:capella")
    assert channel.fetch(project="awm", scope="dev") == []


def test_the_senders_claimed_author_is_kept_in_meta(journal_workspace):
    stamped = _post("peer:capella", author="agent:awm/dev")
    assert stamped["author"] == "peer:capella"
    assert stamped["meta"]["claimed_author"] == "agent:awm/dev"
    assert "claimed_author" not in _post(None, author="agent:awm/dev")["meta"]


def test_a_post_records_the_session_behind_the_stamped_pid(journal_workspace, monkeypatch):
    from awm.journal.operations import posts
    monkeypatch.setattr(posts.roster, "session_by_pid",
                        lambda pid: {"sessionId": "sess-9"} if pid == 42 else None)
    args = {"project": "awm", "scope": "dev", "author": "agent:awm/dev", "body": "x",
            "kind": "journal", "meta": {"title": "t", "session": "forged"}}
    assert posts._handle_post({**args, "_caller_pid": 42})["post"]["meta"] == {
        "title": "t", "session": "sess-9"}
    assert posts._handle_post({**args, "_caller_pid": 7})["post"]["meta"] == {"title": "t"}
    assert posts._handle_post({**args, "meta": None, "_caller_pid": 42})["post"]["meta"] == {
        "session": "sess-9"}


def test_claimed_author_does_not_mutate_the_callers_meta(journal_workspace):
    from awm.journal.operations.posts import _handle_post
    meta = {"title": "t"}
    _handle_post({"project": "awm", "scope": "dev", "author": "agent:awm/dev", "body": "x",
                        "kind": "journal", "meta": meta}, "peer:capella")
    assert meta == {"title": "t"}


# ---------------------------------------------------------------------------
# A peer the gateway admits reads what a local caller reads
# ---------------------------------------------------------------------------

def _fetch(as_, **args):
    from awm.journal.operations.posts import _handle_fetch
    return _handle_fetch({"project": "awm", "scope": "dev", **args}, as_)


def test_a_peer_fetch_sees_every_kind(journal_workspace):
    msg = _post(None, author="agent:awm/dev", body="chatter", kind="message")
    _post(None, author="agent:awm/dev", body="the debrief", kind="journal")
    assert len(_fetch("peer:capella")["posts"]) == len(_fetch(None)["posts"]) == 2
    assert _fetch("peer:capella", kind="message")["posts"][0]["id"] == msg["id"]
    assert _fetch("peer:capella", post_id=msg["id"])["total"] == 1


def test_a_journal_fetch_reads_newest_first_and_messages_oldest_first(journal_workspace):
    import time
    for body in ("first", "second"):
        _post(None, author="agent:awm/dev", body=body, kind="journal")
        _post(None, author="agent:awm/dev", body=body, kind="message")
        time.sleep(0.005)
    bodies = lambda **a: [p["body"] for p in _fetch(None, **a)["posts"]]
    assert bodies(kind="journal") == ["second", "first"]
    assert bodies(kind="journal", order="asc") == ["first", "second"]
    assert bodies(kind="message") == ["first", "second"]


def test_a_peer_reads_goals(journal_workspace):
    from awm.journal.operations.goals import _handle_goal_history, _handle_goal_read, _handle_goal_set
    goal = _handle_goal_set({"objective": "shared aim", "author": "user:tony", "level": "scope",
                             "project": "awm", "scope": "dev"})["goal"]
    assert _handle_goal_read({"project": "awm", "scope": "dev"}, "peer:capella")["total"] == 1
    assert _handle_goal_history({"goal_id": goal["id"]}, "peer:capella")["total"] == 1


def test_goal_writes_carry_the_peer_origin(journal_workspace):
    from awm.journal.operations.goals import _handle_goal_retire, _handle_goal_set
    made = _handle_goal_set({"objective": "ship it", "author": "user:mallory", "level": "scope",
                             "project": "awm", "scope": "dev"}, "peer:capella")["goal"]
    assert made["author"] == "peer:capella"
    local = _handle_goal_set({"objective": "own goal", "author": "user:tony", "level": "scope",
                              "project": "awm", "scope": "dev"})["goal"]
    assert local["author"] == "user:tony"
    tomb = _handle_goal_retire({"goal_id": made["id"], "author": "user:mallory"},
                               "peer:shaula")["tombstone"]
    assert tomb["author"] == "peer:shaula"


# ---------------------------------------------------------------------------
# Every verb declares an effect, and none carries a category
# ---------------------------------------------------------------------------

def _functions():
    from awm.journal.hub_adapter import API_MANIFEST
    return {f["name"]: f for f in API_MANIFEST["functions"]}


def test_every_journal_verb_declares_an_effect():
    from awm.config import EFFECTS
    missing = [n for n, f in _functions().items() if f.get("effect") not in EFFECTS]
    assert missing == []


READS = {"fetch", "goal_read", "goal_history", "export"}


def test_only_the_known_reads_are_reads():
    reads = {n for n, f in _functions().items() if f["effect"] == "read"}
    assert reads == READS


def test_no_journal_verb_carries_a_category():
    assert [n for n, f in _functions().items() if "category" in f] == []


def test_every_published_tool_is_journal_prefixed_and_hidden_verbs_leave_mcp():
    for name, fn in _functions().items():
        assert fn["tool"] == f"journal_{name}"
    hidden = {n for n, f in _functions().items() if f.get("surfaces") == ["cli", "http"]}
    assert hidden == {"export", "reindex"}
