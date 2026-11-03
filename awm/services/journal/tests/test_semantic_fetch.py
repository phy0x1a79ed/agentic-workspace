"""journal_fetch ranks posts with the search service, or by substring when search cannot answer."""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.journal]


@pytest.fixture
def posts(journal_workspace):
    from awm.journal import channel
    a = channel.post("awm", "dev", author="agent:awm/dev", kind="journal", body="the telescope mirror cracked")
    b = channel.post("awm", "dev", author="agent:awm/dev", kind="journal", body="unrelated lunch plans")
    return a, b


def test_search_ranks_the_journals_source(posts, fake_search):
    from awm.journal import channel
    a, _ = posts
    got, degraded, semantic = channel.search(query="telescope mirror", project="awm", kind="journal",
                                             before_ts="2999-01-01T00:00:00Z", limit=5)
    assert semantic == "search" and degraded is None
    assert [p.id for p in got] == [a.id] and got[0].match["snippet"] == "the telescope mirror cracked"
    assert fake_search.calls == [{"query": "telescope mirror", "sources": ["journals"], "limit": 5,
                                  "project": "awm", "kind": ["journal"], "before": "2999-01-01T00:00:00Z"}]


def test_filters_search_lacks_still_hold(posts, fake_search):
    from awm.journal import channel
    other = channel.post("awm", "dev", author="user:a", kind="message", body="telescope mirror again")
    got, _, _ = channel.search(query="telescope mirror", author="user:a")
    assert [p.id for p in got] == [other.id]
    assert "author" not in fake_search.calls[-1] and fake_search.calls[-1]["limit"] == 100


def test_offset_pages_the_ranked_hits(posts):
    from awm.journal import channel
    for i in range(3):
        channel.post("awm", "dev", author="user:a", kind="message", body=f"telescope {i}")
    first, _, _ = channel.search(query="telescope", limit=2)
    second, _, _ = channel.search(query="telescope", limit=2, offset=2)
    assert len(first) == 2 and len(second) == 2 and not {p.id for p in first} & {p.id for p in second}


@pytest.mark.parametrize("status", [{"status": "absent"}, {"status": "error", "error": "boom"}])
def test_a_source_search_cannot_answer_falls_back_to_substring(posts, fake_search, status):
    from awm.journal import channel
    a, _ = posts
    fake_search.status = status
    got, degraded, semantic = channel.search(query="mirror", project="awm")
    assert semantic == "none" and degraded["fallback"] == "keyword"
    assert [p.id for p in got] == [a.id]


def test_search_down_falls_back_to_substring(posts, fake_search):
    from awm.journal import channel
    fake_search.error = ConnectionError("gateway down")
    got, degraded, semantic = channel.search(query="lunch", project="awm")
    assert semantic == "none" and "gateway down" in degraded["error"]
    assert [p.body for p in got] == ["unrelated lunch plans"]


def test_a_kind_search_lacks_falls_back_to_substring_without_asking(posts, fake_search):
    from awm.journal import channel
    channel.post("awm", "dev", author="system", kind="system", body="worktree healed")
    got, _, semantic = channel.search(query="healed", kind="system")
    assert semantic == "none" and [p.body for p in got] == ["worktree healed"]
    assert fake_search.calls == []


def test_reply_reports_semantic_only_for_a_query(posts):
    from awm.journal.operations.posts import _handle_fetch
    assert _handle_fetch({"project": "awm", "query": "telescope"})["semantic"] == "search"
    assert "semantic" not in _handle_fetch({"project": "awm", "scope": "dev"})
