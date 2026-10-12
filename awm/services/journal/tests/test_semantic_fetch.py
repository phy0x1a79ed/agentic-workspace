"""journal_fetch ranks posts with the local index, or by substring for kinds it does not hold."""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.journal]


@pytest.fixture
def posts(journal_workspace):
    from awm.journal import channel, search_index
    a = channel.post("awm", "dev", author="agent:awm/dev", kind="journal", body="the telescope mirror cracked")
    b = channel.post("awm", "dev", author="agent:awm/dev", kind="journal", body="unrelated lunch plans")
    search_index.flush()
    return a, b


def test_local_index_ranks(posts):
    from awm.journal import channel
    a, _ = posts
    got, degraded, semantic = channel.search(query="telescope mirror", project="awm")
    assert semantic == "local" and degraded is None
    assert got[0].id == a.id


def test_a_kind_the_index_lacks_falls_back_to_substring(posts):
    from awm.journal import channel
    _, _, semantic = channel.search(query="telescope", kind="system")
    assert semantic == "none"


def test_reply_reports_semantic_only_for_a_query(posts):
    from awm.journal.operations.posts import _handle_fetch
    assert _handle_fetch({"project": "awm", "query": "telescope"})["semantic"] == "local"
    assert "semantic" not in _handle_fetch({"project": "awm", "scope": "dev"})
