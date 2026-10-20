"""`kb query`'s meaning and graph legs: the entry index's hits as paper hits with quotes, and graph neighbours.

The kb server answers `/related` with entries: a paragraph, an abstract or a title of a paper, with the offsets of its
text in papers.db's passage rows. This module keeps each zone's best papers apart, merges them with the keyword leg
by reciprocal rank (so a paper strong in one zone is not lost to a fusion that cut every zone to 20 first), and
quotes each paper from papers.db by the overlap of the entry's span with a passage's span.

The graph leg walks one hop from the top papers and nearest entities on the Ladybug graph through the server's
`/graph`, and orders what it finds by PageRank. Hop results are not ranked by meaning (precedence 5db3b97b404c877a).
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
NEIGHBOUR_TIMEOUT_S = 3.0
RELATED_TIMEOUT_S = 10.0
MAX_ENTITY_IDS = 300

#: label -> Cypher. Each takes $seeds (paper keys) or $ents (Cognee node ids) and returns paper_id, title, year, rank.
_RETURN = "RETURN DISTINCT q.paper_id, q.title, q.year, q.rank ORDER BY q.rank DESC LIMIT {n}"
NEIGHBOURS = {
    "cited-by or cites": "MATCH (p:Paper)-[:CITES]-(q:Paper) WHERE p.key IN $seeds AND NOT q.key IN $seeds "
                         "AND q.registered " + _RETURN,
    "shares an author": "MATCH (p:Paper)<-[:AUTHORED]-(:Author)-[:AUTHORED]->(q:Paper) WHERE p.key IN $seeds "
                        "AND NOT q.key IN $seeds AND q.registered " + _RETURN,
    "shares an entity": "MATCH (e:Node) WHERE e.id IN $ents MATCH (q:Paper)-[:HAS_CHUNK]->(:Node)-[:EDGE]-(e) "
                        "WHERE NOT q.key IN $seeds AND q.registered " + _RETURN,
}


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
    ranked = [(p, s) for p, s in merge([*by_zone.values(), [h["id"] for h in keyword]]) if p not in skip]
    conns = [c for c in (fastlayer._open_ro(p) for p in layer._stores()) if c is not None]
    try:
        rows: dict[str, sqlite3.Row] = {}
        for conn in conns:
            rows.update(fastlayer._papers(conn, [p for p, _ in ranked if p not in rows]))
        out = []
        for paper, score in ranked:
            row = rows.get(paper)
            if row is None or not filters.keeps(row["ts"]):
                continue
            quotes = _quotes(conns, paper, by_paper.get(paper, []), per_hit)
            kept = {(q["field"], q["start"]) for q in quotes}
            quotes += [q for q in keyword_by_id.get(paper, {}).get("passages", [])
                       if (q["field"], q["start"]) not in kept]
            match = "meaning" if paper in by_paper else "keyword"
            out.append(fastlayer._hit(row, match, score, quotes[:per_hit]))
            if len(out) == limit:
                break
        return out
    finally:
        for conn in conns:
            conn.close()


def describe(layer: fastlayer.FastLayer, found: dict[str, tuple[str, float, str]], filters: fastlayer.Filters,
             limit: int, per_hit: int) -> list[dict[str, Any]]:
    """Graph neighbours as hits: ``found`` maps a paper id to (via, rank, title); the abstract is the quote."""
    conns = [c for c in (fastlayer._open_ro(p) for p in layer._stores()) if c is not None]
    try:
        rows: dict[str, sqlite3.Row] = {}
        for conn in conns:
            rows.update(fastlayer._papers(conn, [p for p in found if p not in rows]))
        out = []
        for paper, (via, rank, _) in sorted(found.items(), key=lambda kv: -kv[1][1]):
            row = rows.get(paper)
            if row is None or not filters.keeps(row["ts"]):
                continue
            quote = next((fastlayer._quote(p) for c in conns for p in c.execute(
                "SELECT * FROM passage WHERE paper_id = ? AND zone = 'abstract' ORDER BY ord LIMIT 1", (paper,))),
                None)
            out.append(fastlayer._hit(row, via, rank, [quote] if quote and per_hit else []))
            if len(out) == limit:
                break
        return out
    finally:
        for conn in conns:
            conn.close()


async def entries_for(text: str) -> dict[str, Any]:
    """The server's `/related` answer for ``text``: entries per zone and the nearest entities."""
    return await client.related({"text": text, "k": ENTRY_K, "zones": list(ZONES), "nouns": NOUNS},
                                timeout=RELATED_TIMEOUT_S)


async def neighbours(seeds: list[str], entities: list[str],
                      limit: int) -> tuple[dict[str, tuple[str, float, str]], list[str]]:
    """Papers one hop from the seed papers and the entities' chunks: paper id -> (relation, PageRank, title),
    and why any hop failed.

    The three hops run at once; a hop that fails or times out adds nothing.
    """
    n = max(limit * 3, 30)
    jobs = {}
    if seeds:
        jobs["cited-by or cites"] = {"seeds": seeds}
        jobs["shares an author"] = {"seeds": seeds}
    if entities:
        jobs["shares an entity"] = {"seeds": seeds, "ents": entities[:MAX_ENTITY_IDS]}
    replies = await asyncio.gather(
        *(client.graph({"cypher": NEIGHBOURS[via].format(n=n), "params": params, "limit": n,
                        "timeout_s": NEIGHBOUR_TIMEOUT_S}, timeout=NEIGHBOUR_TIMEOUT_S + 5)
          for via, params in jobs.items()), return_exceptions=True)
    found: dict[str, tuple[str, float, str]] = {}
    errors: list[str] = []
    for via, reply in zip(jobs, replies):
        if isinstance(reply, BaseException):
            errors.append(f"{via}: {reason(reply)}")
            continue
        for paper_id, title, _year, rank in reply["rows"]:
            if paper_id not in found or (rank or 0.0) > found[paper_id][1]:
                found[paper_id] = (via, float(rank or 0.0), title or "")
    return found, errors


def reason(exc: BaseException) -> str:
    """Why a call to the kb server failed, in a few words."""
    if isinstance(exc, client.KbError):
        return exc.detail[:200]
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return "timed out"
    return f"kb server unreachable ({type(exc).__name__})"
