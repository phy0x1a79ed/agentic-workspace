"""Comments, threads and supersede by reply (fleet.md R107 items 5 and 8)."""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.journal]


def _fetch(**args):
    from awm.journal.operations.posts import POST_HANDLERS
    return POST_HANDLERS["fetch"](args, None)


def _post(**args):
    from awm.journal.operations.posts import POST_HANDLERS
    return POST_HANDLERS["post"](args, None)["post"]


def test_a_comment_lives_in_the_thread_of_its_first_post(journal_workspace):
    root = _post(project="awm", scope="dev", author="agent:awm/dev", tags=["journal"],
                 body="the flaky test was a race")
    c1 = _post(author="agent:other/x", reply_to=root["id"], body="seen it too")
    c2 = _post(project="other", scope="x", author="user:tony", reply_to=c1["id"],
               body="fix it at the lock")
    for c in (c1, c2):
        assert (c["project"], c["scope"]) == ("awm", "dev") and c["thread"] == root["id"]
    assert c2["reply_to"] == [c1["id"]]
    out = _fetch(post_id=root["id"])
    assert out["posts"][0]["comments"] == 2
    [top] = out["thread"]
    assert top["id"] == c1["id"] and [r["id"] for r in top["replies"]] == [c2["id"]]


def test_a_post_needs_a_channel_or_a_reply(journal_workspace):
    with pytest.raises(ValueError, match="reply_to"):
        _post(author="user:tony", body="lost")
    with pytest.raises(ValueError, match="no post"):
        _post(author="user:tony", reply_to="nope", body="lost")


def test_the_first_posts_author_gets_each_comment_in_its_inbox(journal_workspace):
    from awm.journal import inbox
    asked = _post(project="awm", scope="dev", author="agent:other/x", body="can you look?")
    answer = _post(author="agent:awm/dev", reply_to=asked["id"], body="done")
    digest = inbox.digest("other", "x")
    assert [p["id"] for p in digest["posts"]] == [answer["id"]]
    assert digest["posts"][0]["channel"] == "awm/dev"
    inbox.ack("other", "x", through=answer["id"])
    assert inbox.digest("other", "x")["unread"] == 0
    own = _post(author="agent:other/x", reply_to=asked["id"], body="thanks")
    assert own["id"] not in {p["id"] for p in inbox.digest("other", "x")["posts"]}


def test_search_returns_the_first_post_with_its_comment_count(journal_workspace):
    from awm.journal import search_index
    root = _post(project="awm", scope="dev", author="agent:awm/dev", tags=["journal"],
                 body="gateway restart notes")
    _post(author="user:tony", reply_to=root["id"], body="the zeppelin hangar flooded")
    search_index.flush()
    out = _fetch(query="zeppelin hangar", project="awm", limit=5)
    assert [p["id"] for p in out["posts"]] == [root["id"]]
    assert out["posts"][0]["comments"] == 1


def test_export_carries_one_document_per_thread(journal_workspace):
    from awm.journal.export import export_page
    root = _post(project="awm", scope="dev", author="agent:awm/dev", tags=["journal"], body="root")
    _post(author="user:tony", reply_to=root["id"], body="a comment")
    page = export_page()
    assert page["ids"] == [root["id"]]
    doc = page["documents"][0]
    assert doc["comments"] == 1 and "a comment" in doc["text"]


def test_a_goal_revision_replies_and_supersedes(journal_workspace):
    from awm.journal import channel, goals
    a = goals.set_goal(objective="v1", author="user:tony", level="scope", project="awm",
                       scope="dev")
    b = goals.set_goal(objective="v2", author="user:tony", supersedes=a.id)
    assert b.supersedes == a.id and "superseded" in channel.get_post(a.id).tags
    assert channel.get_post(b.id).reply_to == a.id
    assert [g.id for g in goals.read_goals("awm", "dev", levels=["scope"])] == [b.id]


def test_a_goal_revision_that_moves_level_follows_the_switch(journal_workspace, monkeypatch):
    from awm.journal import channel, goals
    a = goals.set_goal(objective="v1", author="user:tony", level="scope", project="awm",
                       scope="dev")
    b = goals.set_goal(objective="v2", author="user:tony", supersedes=a.id, level="workspace")
    assert (b.project, b.scope) == ("", "workspace") and b.supersedes == a.id
    assert [g.id for g in goals.history(a.id)] == [a.id, b.id]
    monkeypatch.setattr(channel, "CROSS_CHANNEL_REVISIONS", False)
    with pytest.raises(goals.GoalError, match="stays on its goal's level"):
        goals.set_goal(objective="v3", author="user:tony", supersedes=b.id, level="project",
                       project="awm")


def test_a_brief_supersedes_by_reply_and_keeps_its_history(journal_workspace):
    from awm.journal import briefs, channel
    one = briefs.set_brief("awm", "dev", "for the gateway", author="agent:awm/dev")
    two = briefs.set_brief("awm", "dev", "for the gateway and its plugins", author="agent:awm/dev")
    assert briefs.read("awm", "dev").id == two.id and two.supersedes == one.id
    assert "superseded" in channel.get_post(one.id).tags
    assert [b.id for b in briefs.history("awm", "dev")] == [one.id, two.id]
    assert channel.get_post(two.id).tags == ["brief"]
