"""Reciprocal rank fusion over the ranked lists the archive, live sources and other nodes return.

Scores from different lists do not compare: the archive's are fused cosine and
BM25, a live source has none, and each peer ranks against its own corpus. Rank
position is the one thing every list has, so it is all that is fused.
"""

from __future__ import annotations

from typing import Any

from awm.search.hits import READ_ONCE, SECTIONS, bare

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


def within(fused: list[dict[str, Any]], anchor: list[dict[str, Any]], cut: int) -> list[dict[str, Any]]:
    """``anchor``'s first ``cut`` hits in ``fused``'s order, then the rest of ``fused``: fusion reorders the
    anchor's head and adds below it, and never pushes one of those hits out of the first ``cut``."""
    head = {_identity(h) for h in anchor[:cut]}
    first = [h for h in fused if _identity(h) in head]
    return first + [h for h in fused if _identity(h) not in head]


def label(hit: dict[str, Any], here: str) -> str:
    """The name a hit's source answers under: bare on this node, ``name@node`` from another."""
    node = hit["origin"]["node"]
    return hit["source"] if node == here else f"{hit['source']}@{node}"


def fuse_sections(found: list[dict[str, list[dict[str, Any]]]], limit: int,
                  here: str) -> dict[str, dict[str, list[dict[str, Any]]]]:
    """Each section fused across ``found`` into one ranked list per source, ``limit`` hits at most each.

    A source is reported on its own: its hits are never ranked against another source's, so papers do not
    crowd out journal posts, and a source on another node answers under ``name@node``. A source's lists from
    this node fuse into one. A document is kept only in the first of :data:`READ_ONCE` that holds it: those
    sections read in order, so a document already shown as an exact match would only repeat itself further
    down. A source with no hits in a section has no entry there. No hit leaves with its passages.
    """
    out: dict[str, dict[str, list[dict[str, Any]]]] = {}
    shown: set[tuple[str, str, str]] = set()
    for section in SECTIONS:
        lists: dict[str, list[list[dict[str, Any]]]] = {}
        for f in found:
            grouped: dict[str, list[dict[str, Any]]] = {}
            for hit in f.get(section, []):
                grouped.setdefault(label(hit, here), []).append(hit)
            for source, hits in grouped.items():
                lists.setdefault(source, []).append(hits)
        once = section in READ_ONCE
        per_source = {s: [bare(h) for h in fuse(lists[s], limit * 2) if not (once and _identity(h) in shown)][:limit]
                      for s in sorted(lists)}
        out[section] = {s: hits for s, hits in per_source.items() if hits}
        if once:
            shown |= {_identity(h) for hits in out[section].values() for h in hits}
    return out
