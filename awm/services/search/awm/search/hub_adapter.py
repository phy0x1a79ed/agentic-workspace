"""Hub adapter for the search service: one ranked search over everything the swarm knows.

Boots the service as a gateway-registered process: stands up its database, then
runs the shared :class:`awm.gatewayclient.ServiceAdapter` loop (register → ready
→ serve → reconnect) beside the archive's sync loop. The read verbs declare
``category: "archive"``, which lets a foreign peer holding at least one grant
reach them; the gateway hands the grants in as ``args["_grants"]`` and the
archive answers only those sources.

Run via ``run.sh`` (which the gateway spawns and respawns):
    python -m awm.search.hub_adapter
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from awm import config
from awm.config.peertoken import node_label
from awm.gatewayclient import ServiceAdapter, spawn_supervised

from awm.search import federation, query as request, store
from awm.search.archive import Archive

log = logging.getLogger("awm.search.hub_adapter")

_CLI_HTTP = ["cli", "http"]

API_MANIFEST: dict[str, Any] = {
    "functions": [
        {
            "name": "query",
            "effect": "read",
            "category": "archive",
            "tool": "search_query",
            "timeout": 60,
            "description": (
                "One search over everything the swarm knows: journals, precedents, papers "
                "and any other source in `search sources`. It searches this node only, unless "
                "`peers` names other nodes. The answer has three ranked sections, read in "
                "order: `exact` (the query as a literal substring, case-insensitive, then "
                "keyword matches), `related` (nearest in meaning) and `graph` (linked through "
                "the knowledge graph). A document appears in the first section that holds it. "
                "Each hit carries source, id, title, ts, score, match, origin (node and swarm) "
                "and its passages: quotes with their section and their start and end offsets in "
                "the document's field. Read the answer's `sources` and `peers` for the status "
                "of each source and peer asked. Fetch a whole hit with `get`."
            ),
            "params": [
                {"name": "query", "type": "string", "required": True,
                 "description": "What to find, in words."},
                {"name": "sources", "type": "array",
                 "description": "Only these sources (names from `sources`). Default: every "
                                "index source and every live source that is not opt-in."},
                {"name": "project", "type": "string", "description": "Only this project's documents."},
                {"name": "scope", "type": "string", "description": "Only this scope's documents."},
                {"name": "kind", "type": "array",
                 "description": "Only these kinds, such as journal or paper."},
                {"name": "after", "type": "string",
                 "description": "Only documents at or after this ISO 8601 date or time (UTC)."},
                {"name": "before", "type": "string",
                 "description": "Only documents before this ISO 8601 date or time (UTC)."},
                {"name": "limit", "type": "integer",
                 "description": "Maximum hits per section, 1-100. Default 10."},
                {"name": "passages", "type": "integer",
                 "description": "Maximum passages quoted per hit, 1-10. Default 1."},
                {"name": "peers", "type": "string",
                 "description": "Where to search. Omit it for this node only. `fleet` adds every "
                                "domestic node. Node names, comma-separated, domestic or foreign, "
                                "search only those nodes. Add `local` to the names to include "
                                "this node."},
                {"name": "context", "type": "string",
                 "description": "The scope to rank for, as project/scope or a project. It reorders each "
                                "section by that scope's PageRank and never changes which hits appear. "
                                "Default: the calling session's scope."},
            ],
        },
        {
            "name": "get",
            "effect": "read",
            "category": "archive",
            "tool": "search_get",
            "description": (
                "One archived document in full: title, text, project, scope, kind, ts and url. "
                "Take `source` and `id` from a hit. For a hit from another node, set `node` to "
                "the hit's origin node."
            ),
            "params": [
                {"name": "source", "type": "string", "required": True, "description": "The hit's source."},
                {"name": "id", "type": "string", "required": True, "description": "The hit's id."},
                {"name": "node", "type": "string",
                 "description": "The node that holds the document, from the hit's origin. Default: this node."},
            ],
        },
        {
            "name": "sources",
            "effect": "read",
            "category": "archive",
            "tool": "search_sources",
            "description": (
                "Every source the archive holds: its service, mode (index or live), document "
                "count, last successful sync, whether it is frozen (its service is not running, "
                "so its last documents are served) and its last error."
            ),
            "params": [],
        },
        {
            "name": "status",
            "effect": "read",
            "category": "archive",
            "tool": "search_status",
            "description": (
                "The archive's health: documents per source, whether semantic ranking is "
                "available, the registry's last read, running sweeps and push subscriptions."
            ),
            "params": [],
        },
        {
            "name": "reindex",
            "effect": "write",
            "tool": "search_reindex",
            "surfaces": _CLI_HTTP,
            "timeout": 600,
            "description": (
                "Re-embed stale documents (all of them with force) and queue a fresh export "
                "pass over one source, or over every index source."
            ),
            "params": [
                {"name": "source", "type": "string", "description": "Only this source's pass."},
                {"name": "force", "type": "boolean", "description": "Re-embed every document."},
            ],
        },
        {
            "name": "judge",
            "effect": "write",
            "tool": "search_judge",
            "description": (
                "Record whether one hit answers one query, for the judgement set that every "
                "change to search is replayed against. A later verdict on the same query and "
                "hit replaces the earlier one, and `clear` removes it."
            ),
            "params": [
                {"name": "query", "type": "string", "required": True, "description": "The query, as asked."},
                {"name": "source", "type": "string", "required": True, "description": "The hit's source."},
                {"name": "id", "type": "string", "required": True, "description": "The hit's id."},
                {"name": "verdict", "type": "string", "required": True,
                 "description": "relevant, irrelevant, or clear."},
                {"name": "section", "type": "string",
                 "description": "For a relevant hit, the section it must answer in: exact, related or graph. "
                                "Default: any."},
                {"name": "set", "type": "string",
                 "description": "The set the judgement belongs to, such as gold or t5. Default: none, a mark by hand."},
                {"name": "note", "type": "string", "description": "Why."},
            ],
        },
        {
            "name": "judgements",
            "effect": "read",
            "tool": "search_judgements",
            "surfaces": _CLI_HTTP,
            "description": "The recorded judgements, optionally of one set or one query.",
            "params": [
                {"name": "set", "type": "string", "description": "Only this set."},
                {"name": "query", "type": "string", "description": "Only this query."},
            ],
        },
    ],
    "emitters": [],
    "sessions": [],
}

ARCHIVE = Archive()


def _grants(args: dict) -> frozenset[str] | None:
    grants = args.get("_grants")
    return None if grants is None else frozenset(str(g) for g in grants)


async def _h_query(args: dict, as_: str | None = None) -> dict:
    return await ARCHIVE.query(request.parse(args, as_))


async def _h_get(args: dict, as_: str | None = None) -> dict:
    node = str(args.get("node") or "").strip()
    if node and node != request.LOCAL and not request.peer_originated(as_) \
            and node_label(node) != node_label(config.node_name()):
        return await federation.fetch(ARCHIVE.peers, node, str(args["source"]), str(args["id"]))
    return await ARCHIVE.get(str(args["source"]), str(args["id"]), _grants(args))


async def _h_sources(args: dict) -> dict:
    return await ARCHIVE.sources_view(_grants(args))


async def _h_status(args: dict) -> dict:
    return await ARCHIVE.status_view(_grants(args))


async def _h_reindex(args: dict) -> dict:
    force = args.get("force") in (True, "true", "True", "1", 1)
    return await ARCHIVE.reindex(args.get("source") or None, force)


def _judge(args: dict, as_: str | None) -> dict:
    query, source, doc_id = (str(args.get(k) or "").strip() for k in ("query", "source", "id"))
    verdict, section = (str(args.get(k) or "").strip().lower() for k in ("verdict", "section"))
    if not (query and source and doc_id):
        raise ValueError("search judge needs a query, a source and an id")
    if verdict not in (*store.VERDICTS, "clear"):
        raise ValueError(f"verdict: {verdict!r} is not relevant, irrelevant or clear")
    if section and section not in store.SECTIONS:
        raise ValueError(f"section: {section!r} is not exact, related or graph")
    conn = store.connect()
    try:
        if verdict == "clear":
            return {"cleared": store.unjudge(conn, query, source, doc_id)}
        return {"judged": store.judge(conn, {
            "query": query, "source": source, "doc_id": doc_id, "verdict": verdict, "section": section,
            "collection": str(args.get("set") or ""), "note": str(args.get("note") or ""),
            "judge": as_ or ""})}
    finally:
        conn.close()


def _judgements(args: dict) -> dict:
    conn = store.connect()
    try:
        return {"judgements": store.judgements(conn, str(args.get("set") or ""), str(args.get("query") or ""))}
    finally:
        conn.close()


async def _h_judge(args: dict, as_: str | None = None) -> dict:
    return await asyncio.to_thread(_judge, args, as_)


async def _h_judgements(args: dict) -> dict:
    return await asyncio.to_thread(_judgements, args)


HANDLERS = {
    "query": _h_query,
    "get": _h_get,
    "sources": _h_sources,
    "status": _h_status,
    "reindex": _h_reindex,
    "judge": _h_judge,
    "judgements": _h_judgements,
}


async def _on_start() -> None:
    await asyncio.to_thread(store.init)
    spawn_supervised("search:archive", ARCHIVE.run)


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    await ServiceAdapter("search", API_MANIFEST, HANDLERS, on_start=_on_start).run()


if __name__ == "__main__":
    asyncio.run(main())
