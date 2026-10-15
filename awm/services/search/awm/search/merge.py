"""Reciprocal rank fusion over the ranked lists the archive, live sources and peers return.

Scores from different lists do not compare: the archive's are fused cosine and
BM25, a live source has none, and each peer ranks against its own corpus. Rank
position is the one thing every list has, so it is all that is fused.
"""

from __future__ import annotations

from typing import Any

RRF_K = 60


def _identity(hit: dict[str, Any]) -> tuple[str, str, str]:
    return (hit["origin"]["node"], hit["source"], hit["id"])


def fuse(lists: list[list[dict[str, Any]]], limit: int) -> list[dict[str, Any]]:
    """The best ``limit`` hits across ``lists``, each list ordered best first.

    A hit seen in several lists keeps its first appearance and gathers every
    list's share of its score. Ties go to the newer hit.
    """
    scores: dict[tuple[str, str, str], float] = {}
    first: dict[tuple[str, str, str], dict[str, Any]] = {}
    for hits in lists:
        for rank, hit in enumerate(hits):
            key = _identity(hit)
            scores[key] = scores.get(key, 0.0) + 1.0 / (RRF_K + rank + 1)
            first.setdefault(key, hit)
    ordered = sorted(scores, key=lambda k: first[k].get("ts") or "", reverse=True)
    ordered.sort(key=lambda k: -scores[k])
    return [{**first[k], "score": round(scores[k], 5)} for k in ordered[:limit]]
