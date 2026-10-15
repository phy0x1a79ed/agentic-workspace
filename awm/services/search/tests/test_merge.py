"""Reciprocal rank fusion over ranked lists."""

from __future__ import annotations

import pytest

from awm.search import merge

pytestmark = [pytest.mark.search]


def hit(source, doc_id, ts="2026-01-01T00:00:00Z", node="local"):
    return {"source": source, "id": doc_id, "ts": ts, "origin": {"node": node, "swarm": "tony"}}


def test_a_hit_in_two_lists_outranks_a_hit_first_in_one():
    a, b, c = hit("j", "a"), hit("j", "b"), hit("j", "c")
    fused = merge.fuse([[a, b], [c, b]], 10)
    assert [h["id"] for h in fused] == ["b", "a", "c"]


def test_a_score_is_the_sum_of_each_lists_share():
    fused = merge.fuse([[hit("j", "a")], [hit("j", "a")]], 10)
    assert fused[0]["score"] == round(2 / (merge.RRF_K + 1), 5)


def test_ties_go_to_the_newer_hit():
    old, new = hit("j", "old", "2025-01-01T00:00:00Z"), hit("j", "new", "2026-01-01T00:00:00Z")
    assert [h["id"] for h in merge.fuse([[old], [new]], 10)] == ["new", "old"]


def test_the_same_id_on_two_nodes_is_two_hits():
    fused = merge.fuse([[hit("j", "a", node="local")], [hit("j", "a", node="altair")]], 10)
    assert len(fused) == 2


def test_the_limit_cuts_the_tail():
    assert len(merge.fuse([[hit("j", str(i)) for i in range(20)]], 5)) == 5


def test_no_lists_is_no_hits():
    assert merge.fuse([[], []], 10) == []


def test_pagerank_lifts_a_hit_it_ranks_far_higher_and_keeps_every_hit():
    hits = [hit("j", str(i)) for i in range(10)]
    reordered = merge.by_rank(hits, [0.0] * 9 + [1.0])
    assert [h["id"] for h in reordered][:7] == ["0", "1", "2", "3", "4", "5", "9"]
    assert sorted(h["id"] for h in reordered) == sorted(h["id"] for h in hits)


def test_pagerank_never_overturns_a_close_section_order():
    hits = [hit("j", "a"), hit("j", "b")]
    assert [h["id"] for h in merge.by_rank(hits, [0.0, 1.0])] == ["a", "b"]


def test_no_ranks_leave_the_section_as_it_was():
    hits = [hit("j", "a"), hit("j", "b")]
    assert merge.by_rank(hits, [None, None]) == hits and merge.by_rank([], []) == []
