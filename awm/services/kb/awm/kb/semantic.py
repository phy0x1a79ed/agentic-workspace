"""`kb query`'s meaning leg: the entry index's hits as paper hits; and `kb hop`'s papers as hits.

The kb server answers `/related` with entries: a paragraph, an abstract or a title of a paper. This module keeps
each zone's best papers apart and merges them with the keyword leg by reciprocal rank, so a paper strong in one zone
is not lost to a fusion that cut every zone to 20 first.
"""

from __future__ import annotations

import asyncio
import sqlite3
from typing import Any, Iterable

from awm.kb import client, fastlayer

ZONES = ("body", "abstract", "title")
#: Papers kept from each zone before the merge, at least.
ZONE_KEEP = 20
RRF_K = 60
#: Entries asked of the index per zone.
ENTRY_K = 60
NOUNS = 3
RELATED_TIMEOUT_S = 10.0

def zone_lists(entries: list[dict[str, Any]], keep: int) -> dict[str, list[str]]:
    """Each zone's papers, best first, a paper once at its best entry."""
    out: dict[str, list[str]] = {}
    for zone in ZONES:
        seen: dict[str, float] = {}
        for e in entries:
            if e["zone"] == zone and e["source"] == "paper":
                seen[e["doc"]] = max(e["score"], seen.get(e["doc"], e["score"]))
        out[zone] = [d for d, _ in sorted(seen.items(), key=lambda kv: -kv[1])][:keep]
    return out


def merge(lists: Iterable[list[str]]) -> list[tuple[str, float]]:
    """Papers by summed reciprocal rank over the lists, best first; ties keep the first list's order."""
    scores: dict[str, float] = {}
    for ranked in lists:
        for rank, paper in enumerate(ranked):
            scores[paper] = scores.get(paper, 0.0) + 1.0 / (RRF_K + rank + 1)
    return sorted(scores.items(), key=lambda kv: -kv[1])


def paper_hits(layer: fastlayer.FastLayer, entries: list[dict[str, Any]], keyword: list[dict[str, Any]],
               skip: set[str], filters: fastlayer.Filters, limit: int) -> list[dict[str, Any]]:
    """The `related` section: the keyword leg and every zone's papers merged. ``skip`` holds the papers the
    `exact` section already shows."""
    by_zone = zone_lists(entries, max(limit, ZONE_KEEP))
    meant = {e["doc"] for e in entries if e["source"] == "paper"}
    ranked = merge([*by_zone.values(), [h["id"] for h in keyword]])
    ranked = [(p, s) for p, s in ranked if p not in skip]
    stores = layer._stores()
    rows = _paper_rows(stores, [p for p, _ in ranked])
    shown = [(p, s) for p, s in ranked if p in rows and filters.admits(rows[p])][:limit]
    return [fastlayer._hit(rows[p], "meaning" if p in meant else "keyword", s) for p, s in shown]


def _paper_rows(stores: list, ids: list[str]) -> dict[str, sqlite3.Row]:
    rows: dict[str, sqlite3.Row] = {}
    for conn in [c for c in (fastlayer._open_ro(p) for p in stores) if c is not None]:
        try:
            rows.update(fastlayer._papers(conn, [p for p in ids if p not in rows]))
        finally:
            conn.close()
    return rows


def describe(layer: fastlayer.FastLayer, found: dict[str, tuple[str, float, str]], filters: fastlayer.Filters,
             limit: int) -> list[dict[str, Any]]:
    """Hop papers as hits: ``found`` maps a paper id to (via, rank, title), in hop order."""
    stores = layer._stores()
    rows = _paper_rows(stores, list(found))
    shown = [(p, v) for p, v in sorted(found.items(), key=lambda kv: -kv[1][1])
             if p in rows and filters.admits(rows[p])][:limit]
    return [fastlayer._hit(rows[paper], via, rank) for paper, (via, rank, _) in shown]


async def entries_for(text: str) -> dict[str, Any]:
    """The server's `/related` answer for ``text``: entries per zone and the nearest entities."""
    return await client.related({"text": text, "k": ENTRY_K, "zones": list(ZONES), "nouns": NOUNS},
                                timeout=RELATED_TIMEOUT_S)


def reason(exc: BaseException) -> str:
    """Why a call to the kb server failed, in a few words."""
    if isinstance(exc, client.KbError):
        return exc.detail[:200]
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return "timed out"
    return f"kb server unreachable ({type(exc).__name__})"
