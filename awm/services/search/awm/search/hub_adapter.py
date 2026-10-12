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

from awm.gatewayclient import ServiceAdapter, spawn_supervised

from awm.search import query as request, store
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
                "One ranked search over everything the swarm knows: journals, precedents, "
                "papers and any other source in `search sources`, on this node and across "
                "peers. Each hit carries source, id, title, snippet, ts, score and origin "
                "(node and swarm). Read the answer's `sources` and `peers` for the status "
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
                {"name": "kind", "type": "string", "description": "Only this kind, such as journal."},
                {"name": "after", "type": "string",
                 "description": "Only documents at or after this ISO 8601 date or time (UTC)."},
                {"name": "before", "type": "string",
                 "description": "Only documents before this ISO 8601 date or time (UTC)."},
                {"name": "limit", "type": "integer", "description": "Maximum hits, 1-100. Default 10."},
                {"name": "peers", "type": "boolean",
                 "description": "Also ask the peers in the book. Default true; false searches "
                                "this node only."},
            ],
        },
        {
            "name": "get",
            "effect": "read",
            "category": "archive",
            "tool": "search_get",
            "description": (
                "One archived document in full: title, text, project, scope, kind, ts and url. "
                "Take `source` and `id` from a hit. A hit from another node is fetched with "
                "this verb's `peer` argument set to the hit's origin node."
            ),
            "params": [
                {"name": "source", "type": "string", "required": True, "description": "The hit's source."},
                {"name": "id", "type": "string", "required": True, "description": "The hit's id."},
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


async def _h_get(args: dict) -> dict:
    return await ARCHIVE.get(str(args["source"]), str(args["id"]), _grants(args))


async def _h_sources(args: dict) -> dict:
    return await ARCHIVE.sources_view(_grants(args))


async def _h_status(args: dict) -> dict:
    return await ARCHIVE.status_view(_grants(args))


async def _h_reindex(args: dict) -> dict:
    force = args.get("force") in (True, "true", "True", "1", 1)
    return await ARCHIVE.reindex(args.get("source") or None, force)


HANDLERS = {
    "query": _h_query,
    "get": _h_get,
    "sources": _h_sources,
    "status": _h_status,
    "reindex": _h_reindex,
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
