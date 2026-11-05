"""Reciprocal rank fusion over the ranked lists one source's answer is built from.

Scores from different lists do not compare: the archive's are fused cosine and
BM25, and kb's entry index scores against its own passages. Rank position is
the one thing every list has, so it is all that is fused.
"""

from __future__ import annotations

from typing import Any

RRF_K = 60


def fuse(lists: list[list[dict[str, Any]]], limit: int) -> list[dict[str, Any]]:
    """The best ``limit`` hits across ``lists``, each list ordered best first.

    A hit seen in several lists keeps its first appearance and gathers every
    list's share of its score. Ties go to the newer hit.
    """
    scores: dict[str, float] = {}
    first: dict[str, dict[str, Any]] = {}
    for hits in lists:
        for rank, hit in enumerate(hits):
            scores[hit["id"]] = scores.get(hit["id"], 0.0) + 1.0 / (RRF_K + rank + 1)
            first.setdefault(hit["id"], hit)
    ordered = sorted(scores, key=lambda k: first[k].get("ts") or "", reverse=True)
    ordered.sort(key=lambda k: -scores[k])
    return [first[k] for k in ordered[:limit]]


def within(fused: list[dict[str, Any]], anchor: list[dict[str, Any]], cut: int) -> list[dict[str, Any]]:
    """``anchor``'s first ``cut`` hits in ``fused``'s order, then the rest of ``fused``: fusion reorders the
    anchor's head and adds below it, and never pushes one of those hits out of the first ``cut``."""
    head = {h["id"] for h in anchor[:cut]}
    return [h for h in fused if h["id"] in head] + [h for h in fused if h["id"] not in head]
