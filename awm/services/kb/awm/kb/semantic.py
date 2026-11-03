"""`kb query`'s meaning and graph legs: the entry index's hits as paper hits with quotes, and graph neighbours.

The kb server answers `/related` with entries: a paragraph, an abstract or a title of a paper, with the offsets of its
text in papers.db's passage rows. This module keeps each zone's best papers apart, merges them with the keyword leg
by reciprocal rank (so a paper strong in one zone is not lost to a fusion that cut every zone to 20 first), and
quotes each paper from papers.db by the overlap of the entry's span with a passage's span.

The graph section's hops come from ``awm.kb.hops``; this module only quotes what they find.
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
SEEDS = 5
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


def _overlap(row: sqlite3.Row, start: int, end: int) -> int:
    return min(row["end"], end) - max(row["start"], start)


def _quotes(conns: list[sqlite3.Connection], paper: str, entries: list[dict[str, Any]],
            per_hit: int) -> list[dict[str, Any]]:
    """The passage each of the paper's best entries falls in, best entry first, a passage once."""
    out, seen = [], set()
    for e in entries:
        for conn in conns:
            rows = conn.execute("SELECT * FROM passage WHERE paper_id = ? AND zone = ? AND start < ? AND end > ?",
                                (paper, e["part"], e["end"], e["start"])).fetchall()
            if rows:
                best = max(rows, key=lambda r: _overlap(r, e["start"], e["end"]))
                if best["id"] not in seen:
                    seen.add(best["id"])
                    out.append(fastlayer._quote(best))
                break
        if len(out) >= per_hit:
            break
    return out


def paper_hits(layer: fastlayer.FastLayer, entries: list[dict[str, Any]], keyword: list[dict[str, Any]],
               skip: set[str], filters: fastlayer.Filters, limit: int, per_hit: int) -> list[dict[str, Any]]:
    """The `related` section: the keyword leg and every zone's papers merged, each paper quoted.

    A paper the keyword leg found keeps its quotes beside the meaning matches' ones. ``skip`` holds the papers the
    `exact` section already shows.
    """
    by_zone = zone_lists(entries, max(limit, ZONE_KEEP))
    by_paper: dict[str, list[dict[str, Any]]] = {}
    for e in sorted(entries, key=lambda e: -e["score"]):
        if e["source"] == "paper":
            by_paper.setdefault(e["doc"], []).append(e)
    keyword_by_id = {h["id"]: h for h in keyword}
    ranked = merge([*by_zone.values(), [h["id"] for h in keyword]])
    ranked = [(p, s) for p, s in ranked if p not in skip]
    stores = layer._stores()
    rows = _paper_rows(stores, [p for p, _ in ranked])
    shown = [(p, s) for p, s in ranked if p in rows and filters.admits(rows[p])][:limit]
    quotes = _spread(stores, lambda conns, part: {p: _quotes(conns, p, by_paper.get(p, []), per_hit) for p in part},
                     [p for p, _ in shown])
    out = []
    for paper, score in shown:
        kept = {(q["field"], q["start"]) for q in quotes[paper]}
        extra = [q for q in keyword_by_id.get(paper, {}).get("passages", []) if (q["field"], q["start"]) not in kept]
        match = "meaning" if paper in by_paper else "keyword"
        out.append(fastlayer._hit(rows[paper], match, score, (quotes[paper] + extra)[:per_hit]))
    return out


def _paper_rows(stores: list, ids: list[str]) -> dict[str, sqlite3.Row]:
    rows: dict[str, sqlite3.Row] = {}
    for conn in [c for c in (fastlayer._open_ro(p) for p in stores) if c is not None]:
        try:
            rows.update(fastlayer._papers(conn, [p for p in ids if p not in rows]))
        finally:
            conn.close()
    return rows


def _spread(stores: list, job, items: list[str]) -> dict[str, Any]:
    """``job(conns, part)`` over ``items``, :data:`fastlayer.READ_DEPTH` parts at once, each part on connections
    of its own to every store; each per-paper read is a cold index descent, so they run side by side."""
    if not items:
        return {}

    def run(part):
        conns = [c for c in (fastlayer._open_ro(p) for p in stores) if c is not None]
        try:
            return job(conns, part)
        finally:
            for conn in conns:
                conn.close()

    step = -(-len(items) // fastlayer.READ_DEPTH)
    out: dict[str, Any] = {}
    for f in [fastlayer._reads.submit(run, items[i:i + step]) for i in range(0, len(items), step)]:
        out.update(f.result())
    return out


def _abstracts(conns: list[sqlite3.Connection], papers: list[str]) -> dict[str, dict[str, Any] | None]:
    return {paper: next((fastlayer._quote(p) for c in conns for p in c.execute(
        "SELECT * FROM passage WHERE paper_id = ? AND zone = 'abstract' ORDER BY ord LIMIT 1", (paper,))), None)
        for paper in papers}


def describe(layer: fastlayer.FastLayer, found: dict[str, tuple[str, float, str]], filters: fastlayer.Filters,
             limit: int, per_hit: int) -> list[dict[str, Any]]:
    """Graph neighbours as hits: ``found`` maps a paper id to (via, rank, title); the abstract is the quote, or
    the title for a paper known only from a citation."""
    stores = layer._stores()
    rows = _paper_rows(stores, list(found))
    shown = [(p, v) for p, v in sorted(found.items(), key=lambda kv: -kv[1][1])
             if p in rows and filters.admits(rows[p])][:limit]
    quotes = _spread(stores, _abstracts, [p for p, _ in shown]) if per_hit else {}
    out = []
    for paper, (via, rank, _) in shown:
        row, quote = rows[paper], quotes.get(paper)
        if quote is None and row["title"]:
            quote = {"field": "title", "start": 0, "end": len(row["title"]), "text": row["title"]}
        out.append(fastlayer._hit(row, via, rank, [quote] if quote and per_hit else []))
    return out


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
