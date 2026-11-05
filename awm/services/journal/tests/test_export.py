"""journal_export: the change feed the search archive reads."""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.journal]


def _all(since=None, limit=None):
    from awm.journal.feed import changes
    out, page = [], changes(since, limit)
    out += page["changes"]
    while not page["done"]:
        page = changes(page["next"], limit)
        out += page["changes"]
    return out, page


@pytest.fixture()
def posted(journal_workspace):
    from awm.journal import channel
    ids = []
    for i in range(5):
        ids.append(channel.post("awm", "dev", author="agent:awm/dev", tags="journal",
                                body=f"entry {i}\nmore", meta={"title": f"Title {i}"}).id)
    channel.post("awm", "dev", author="system", tags="brief", body="not indexed")
    channel.post("awm", "dev", author="user:a", tags="message", body="")
    return ids


def test_the_feed_carries_every_indexed_thread_with_its_entry(posted):
    got, last = _all()
    live = {c["id"]: c["entry"] for c in got if "entry" in c}
    assert set(live) == set(posted)
    entry = live[posted[0]]
    assert set(entry) == {"title", "text", "ts", "project", "scope", "tag", "replies_to"}
    assert entry["title"] == "Title 0" and entry["text"].startswith("entry 0")
    assert entry["project"] == ["awm"] and entry["scope"] == ["dev"] and entry["tag"] == ["journal"]
    assert entry["ts"].endswith("+00:00") and entry["replies_to"] == []
    assert last["done"] and last["epoch"]


def test_a_reader_sees_only_what_changed_after_its_cursor(posted):
    from awm.journal import channel, versions
    _, head = _all()
    versions.edit(posted[1], editor="user:a", body="entry one, edited")
    channel.post("awm", "dev", author="user:b", body="a comment", reply_to=posted[3])
    got, _ = _all(head["next"])
    assert [c["id"] for c in got] == [posted[1], posted[3]]
    assert "a comment" in got[1]["entry"]["text"]


def test_retag_and_tag_rename_bump_the_threads_they_touch(posted):
    from awm.journal import channel, tags
    tags.add("cache", "about caches")
    channel.set_tags(posted[2], add=["cache"])
    _, head = _all()
    tags.edit("cache", name="caching")
    got, _ = _all(head["next"])
    assert [c["id"] for c in got] == [posted[2]]
    assert "caching" in got[0]["entry"]["tag"]


def test_a_deleted_or_folded_thread_goes_out_without_an_entry(posted, journal_conn):
    from awm.journal import versions
    _, head = _all()
    journal_conn.execute("DELETE FROM scope_posts WHERE id=?", (posted[0],))
    journal_conn.commit()
    versions.fold(posted[4], posted[3], editor="user:a")
    got, _ = _all(head["next"])
    by_id = {c["id"]: c for c in got}
    assert "entry" not in by_id[posted[0]] and "entry" not in by_id[posted[4]]
    assert "Correction" in by_id[posted[3]]["entry"]["text"]


def test_a_tag_rename_over_many_threads_pages_without_loss(posted):
    from awm.journal import channel, tags
    tags.add("bulk", "many threads")
    for pid in posted:
        channel.set_tags(pid, add=["bulk"])
    _, head = _all()
    tags.edit("bulk", name="bulk2")
    got, _ = _all(head["next"], limit=2)
    assert sorted(c["id"] for c in got) == sorted(posted)


def test_a_journal_carries_the_journals_it_cites_as_replies_to(posted):
    from awm.journal import channel
    cited = channel.post("awm", "dev", author="agent:awm/dev", tags="journal", body="builds on two",
                         reply_to=[posted[2], posted[0]]).id
    got, _ = _all()
    entry = next(c["entry"] for c in got if c["id"] == cited)
    assert entry["replies_to"] == [posted[2], posted[0]]


def test_a_project_channel_goes_out_as_its_project_with_no_scope(journal_workspace, journal_conn):
    from awm.journal import channel
    pid = channel.post("", "project:awm", author="user:a", tags="journal", body="channel note").id
    got, _ = _all()
    entry = next(c["entry"] for c in got if c["id"] == pid)
    assert entry["project"] == ["awm"] and entry["scope"] == []


def test_a_cursor_past_the_head_asks_for_a_reset(posted):
    from awm.journal.feed import changes
    assert changes("999999:")["reset"] is True


def test_the_seed_migration_numbers_every_indexed_root(posted, journal_conn):
    from awm.journal.dao import FEED_SEED_SQL
    journal_conn.execute("DELETE FROM search_feed")
    journal_conn.executescript(FEED_SEED_SQL)
    got, _ = _all()
    assert {c["id"] for c in got if "entry" in c} == set(posted)


def test_the_handler_and_the_manifest_agree(posted):
    from awm.journal.operations.render import RENDER_HANDLERS, RENDER_MANIFEST_FUNCTIONS
    fn = next(f for f in RENDER_MANIFEST_FUNCTIONS if f["name"] == "export")
    assert fn["tool"] == "journal_export" and fn["surfaces"] == ["cli", "http"]
    assert RENDER_HANDLERS["export"]({"limit": 2})["done"] is False
