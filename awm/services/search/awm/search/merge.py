"""Reciprocal rank fusion over the ranked lists the archive, live sources and peers return.

Scores from different lists do not compare: the archive's are fused cosine and
BM25, a live source has none, and each peer ranks against its own corpus. Rank
position is the one thing every list has, so it is all that is fused.
"""

from __future__ import annotations

from typing import Any

from awm.search.hits import SECTIONS

RRF_K = 60
#: PageRank's share of a section's order, against the section's own rank at 1.
RANK_WEIGHT = 0.5


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


def by_rank(hits: list[dict[str, Any]], ranks: list[float | None]) -> list[dict[str, Any]]:
    """``hits`` reordered by their place in the section fused with their place by PageRank; never refiltered."""
    if not hits or all(r is None for r in ranks):
        return hits
    by_pr = sorted(range(len(hits)), key=lambda i: -(ranks[i] or 0.0))
    place = {i: p for p, i in enumerate(by_pr)}
    fused = {i: 1.0 / (RRF_K + i + 1) + RANK_WEIGHT / (RRF_K + place[i] + 1) for i in range(len(hits))}
    return [hits[i] for i in sorted(range(len(hits)), key=lambda i: -fused[i])]


def fuse_sections(found: list[dict[str, list[dict[str, Any]]]],
                  limit: int) -> dict[str, dict[str, list[dict[str, Any]]]]:
    """Each section fused across ``found`` into one ranked list per source, ``limit`` hits at most each.

    A source is reported on its own: its hits are never ranked against another source's, so papers do not
    crowd out journal posts. A source's lists from several places (this node, a peer, a live source) fuse
    into one. A document is kept only in the first section that holds it: the sections read in order, so a
    document already quoted as an exact match would only repeat itself further down. A source with no hits in
    a section has no entry there.
    """
    out: dict[str, dict[str, list[dict[str, Any]]]] = {}
    shown: set[tuple[str, str, str]] = set()
    for section in SECTIONS:
        lists: dict[str, list[list[dict[str, Any]]]] = {}
        for f in found:
            grouped: dict[str, list[dict[str, Any]]] = {}
            for hit in f.get(section, []):
                grouped.setdefault(hit["source"], []).append(hit)
            for source, hits in grouped.items():
                lists.setdefault(source, []).append(hits)
        per_source = {s: [h for h in fuse(lists[s], limit * 2) if _identity(h) not in shown][:limit]
                      for s in sorted(lists)}
        out[section] = {s: hits for s, hits in per_source.items() if hits}
        shown |= {_identity(h) for hits in out[section].values() for h in hits}
    return out
