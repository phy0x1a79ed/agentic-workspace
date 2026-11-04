"""The tag vocabulary, tag filters and batch retagging (fleet.md R107 items 6 and 7)."""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.journal]


@pytest.fixture()
def vocab(journal_workspace, monkeypatch):
    from awm.journal import tags, tagsync
    sent = []
    monkeypatch.setattr(tagsync, "peers", lambda: ["altair"])
    monkeypatch.setattr(tagsync, "sender", lambda peer, t: sent.append((peer, t)))
    tags.add("complaint", "Something in awm that got in the way, like a bug report.")
    return sent


def _post(**kw):
    from awm.journal import channel
    kw.setdefault("author", "agent:awm/dev")
    kw.setdefault("body", "text")
    return channel.post("awm", "dev", **kw)


def test_the_fixed_tags_are_seeded(journal_workspace):
    from awm.journal import tags
    names = {t.name for t in tags.vocabulary()}
    assert {"message", "journal", "brief", "closed", "superseded", "precedent"} <= names
    assert "goal" not in names
    assert tags.by_name("precedent").description.startswith("A decision Tony made or overrode")


def test_an_add_needs_a_description_and_a_new_name(vocab):
    from awm.journal import tags
    with pytest.raises(tags.TagError, match="description"):
        tags.add("bug", "")
    with pytest.raises(tags.TagError, match="exists"):
        tags.add("Complaint", "again")
    with pytest.raises(tags.TagError, match="not a tag name"):
        tags.add("two words", "x")
    with pytest.raises(tags.TagError, match="not a tag name"):
        tags.add("and", "x")


def test_an_add_and_an_edit_go_to_every_peer(vocab):
    from awm.journal import tags
    tags.edit("complaint", description="A bug report or a gripe.")
    assert [(p, t[0]["version"]) for p, t in vocab] == [("altair", 1), ("altair", 2)]


def test_a_post_with_an_unknown_tag_is_refused_with_the_vocabulary(vocab):
    from awm.journal import tags
    with pytest.raises(tags.TagError, match="unknown tag 'bugz'.*complaint.*journal"):
        _post(tags=["bugz"])


def test_a_post_with_no_tag_is_a_message_and_kind_is_derived(vocab):
    p = _post()
    assert p.tags == ["message"] and p.kind == "message"
    j = _post(tags=["journal", "complaint"])
    assert set(j.tags) == {"journal", "complaint"} and j.to_dict()["kind"] == "journal"
    assert _post(kind="journal").tags == ["journal"]


def test_a_rename_shows_on_every_post_and_keeps_the_versions(vocab):
    from awm.journal import channel, tags
    p = _post(tags=["complaint"])
    tags.edit("complaint", name="gripe")
    assert "gripe" in channel.get_post(p.id).tags
    assert [v["name"] for v in tags.versions("gripe")] == ["complaint", "gripe"]
    tags.edit("journal", name="session-log")
    assert channel.get_post(_post(kind="journal").id).kind == "journal"
    assert [v["name"] for v in tags.versions("session-log")] == ["journal", "session-log"]


def test_filters_take_negation_and_conjunction(vocab):
    from awm.journal import channel
    open_c = _post(tags=["complaint"])
    shut = _post(tags=["complaint"])
    channel.set_tags(shut.id, add=["closed"])
    _post(tags=["journal"])
    got = channel.fetch(project="awm", scope="dev", tags="!closed and complaint")
    assert [p.id for p in got] == [open_c.id]
    assert [p.id for p in channel.fetch(project="awm", scope="dev", tags=["closed"])] == [shut.id]
    with pytest.raises(ValueError, match="unknown tag 'nope'"):
        channel.fetch(project="awm", scope="dev", tags="nope and !closed")


def test_one_call_adds_and_removes_several_tags(vocab):
    from awm.journal import channel, tags
    tags.add("triaged", "Someone read it and sorted it.")
    p = _post(tags=["complaint"])
    after = channel.set_tags(p.id, add=["closed", "triaged"], remove=["complaint"])
    assert set(after.tags) == {"message", "closed", "triaged"}
    with pytest.raises(tags.TagError, match="both added and removed"):
        channel.set_tags(p.id, add=["closed"], remove=["closed"])


@pytest.mark.parametrize("name", ["message", "journal", "brief", "precedent"])
def test_a_type_tag_is_set_only_when_the_post_is_made(vocab, name):
    from awm.journal import channel, tags
    p = _post()
    with pytest.raises(tags.TagError, match="set when a post is written"):
        channel.set_tags(p.id, add=[name] if name != "message" else [], remove=[name] if name == "message" else [])


def test_the_retag_verb_refuses_a_foreign_peer(vocab, monkeypatch):
    from awm.journal import channel
    from awm.journal.operations.tags import TAG_HANDLERS
    monkeypatch.setattr(channel, "is_foreign", lambda as_: as_ == "peer:x")
    p = _post()
    with pytest.raises(PermissionError):
        TAG_HANDLERS["retag"]({"post_id": p.id, "add": ["closed"]}, "peer:x")
    out = TAG_HANDLERS["retag"]({"post_id": p.id, "add": ["closed"]}, None)
    assert "closed" in out["post"]["tags"]


def test_tag_list_shows_each_tag_with_its_description(vocab):
    from awm.journal.operations.tags import TAG_HANDLERS
    out = TAG_HANDLERS["tag_list"]({}, None)
    by = {t["name"]: t for t in out["tags"]}
    assert by["complaint"]["description"].startswith("Something")
    assert out["clashes"] == []


def test_inbox_takes_a_tag_filter(vocab):
    from awm.journal import inbox
    _post(author="user:tony", tags=["complaint"], body="the render is slow")
    _post(author="user:tony", body="hello")
    assert inbox.digest("awm", "dev")["unread"] == 2
    assert inbox.digest("awm", "dev", tags="complaint")["unread"] == 1
    assert inbox.digest("awm", "dev", kind="journal")["unread"] == 0


def test_a_post_tagged_with_a_marked_project_is_hidden_from_a_peer(vocab, journal_workspace):
    from awm.config import privacy
    from awm.journal import channel, private, tags
    tags.add("avatar", "Work on the avatar project.")
    hidden = _post(tags=["journal", "avatar"], body="about avatar")
    shown = _post(tags=["journal"], body="about awm")
    privacy.write_marks([{"project": "avatar", "scope": None,
                          "path": str(journal_workspace["projects_dir"] / "avatar")}])
    got = {p.id for p in channel.fetch(project="awm", scope="dev", exclude=private.exclusion("peer"))}
    assert got == {shown.id}
    assert private.hides_post("peer", channel.get_post(hidden.id))
    assert not private.hides_post(None, channel.get_post(hidden.id))
