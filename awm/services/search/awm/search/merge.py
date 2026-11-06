"""Reciprocal rank fusion over the ranked lists one source's answer is built from.

A live source answers in two sections whose scores do not compare. Rank position
is the one thing both lists have, so it is all that is fused.
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

