"""Hub adapter for the search service: one ranked search over everything the fleet knows.

Boots the service as a gateway-registered process: stands up its database, then
runs the shared :class:`awm.gatewayclient.ServiceAdapter` loop (register → ready
→ serve → reconnect) beside the archive's sync loop.

Run via ``run.sh`` (which the gateway spawns and respawns):
    python -m awm.search.hub_adapter
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from awm import config
from awm.config import privacy
from awm.config.peertoken import node_label
from awm.gatewayclient import ServiceAdapter, spawn_supervised
from awm.persistence import embeddings

from awm.search import federation, query as request, store
from awm.search.archive import Archive

log = logging.getLogger("awm.search.hub_adapter")

_CLI_HTTP = ["cli", "http"]

API_MANIFEST: dict[str, Any] = {
    "functions": [
        {
            "name": "query",
            "effect": "read",
            "tool": "search_query",
            "timeout": 60,
            "description": (
                "One search over the sources you name in `sources`: journals, precedents, papers "
                "or any other source `search sources` lists. No source is searched unless named. "
                "It searches this node only, unless "
                "`peers` names other nodes. The answer has two ranked sections, read in order: "
                "`exact` (the query as a literal substring, case-insensitive, then keyword "
                "matches) and `related` (nearest in meaning). Each section reports each source "
                "on its own: a map from source name to that source's ranked hits, up to `limit` "
                "each, so papers (source `kb`), journals, precedents and notes are never ranked "
                "against one another; a source with no hits is absent. A document appears in the "
                "first section that holds it. Each hit carries source, id, title, ts (its date), "
                "project and scope where it has them, score, match and origin (the node that holds "
                "it), and no text: read a hit with `get`, passing the query it answered. Read the answer's "
                "`sources` and `peers` for the status of each source and peer asked. "
                "For papers that cite, are cited by or share an author with paper hits, pass their "
                "ids to `kb hop`. `author` narrows papers to an author."
            ),
            "params": [
                {"name": "query", "type": "string", "required": True,
                 "description": "What to find, in words."},
                {"name": "sources", "type": "array", "required": True,
                 "description": "The sources to search: names from `search sources`, such as "
                                "journals, precedence or kb (papers)."},
                {"name": "project", "type": "string", "description": "Only this project's documents."},
                {"name": "scope", "type": "string", "description": "Only this scope's documents."},
                {"name": "kind", "type": "array",
                 "description": "Only these kinds, such as journal or paper."},
                {"name": "after", "type": "string",
                 "description": "Only documents at or after this ISO 8601 date or time (UTC)."},
                {"name": "before", "type": "string",
                 "description": "Only documents before this ISO 8601 date or time (UTC)."},
                {"name": "limit", "type": "integer",
                 "description": "Maximum hits per source in each section, 1-100. Default 10."},
                {"name": "passages", "type": "integer",
                 "description": "Ignored: a hit quotes no text. Accepted from nodes on an older release."},
                {"name": "peers", "type": "string",
                 "description": "Where to search. Omit it for this node only. `fleet` adds every "
                                "node this node has an edge to. Node names, comma-separated, "
                                "search only those nodes. Add `local` to the names to include "
                                "this node. `fleet` among the names adds every node this node "
                                "has an edge to, as in `fleet,shaula`."},
                {"name": "context", "type": "string",
                 "description": "The scope the search is made from, as project/scope. Default: the "
                                "calling session's scope. No order depends on it."},
                {"name": "author", "type": "string",
                 "description": "Only papers with an author of this surname, matched loosely, so "
                                "'Huttenhauer' finds Huttenhower. Other sources answer nothing."},
            ],
        },
        {
            "name": "get",
            "effect": "read",
            "tool": "search_get",
            "description": (
                "One document in full: title, text, project, scope, kind, ts and url; a paper "
                "adds its authors and journal. Take `source` and `id` from a hit. For a hit from "
                "another node, set `node` to the hit's origin node. Pass the `query` the hit "
                "answered: each get is logged with the caller's scope as feedback on search."
            ),
            "params": [
                {"name": "source", "type": "string", "required": True, "description": "The hit's source."},
                {"name": "id", "type": "string", "required": True, "description": "The hit's id."},
                {"name": "node", "type": "string",
                 "description": "The node that holds the document, from the hit's origin. Default: this node."},
                {"name": "query", "type": "string", "description": "The query whose hit this is."},
                {"name": "context", "type": "string",
                 "description": "The scope the get is made from, as project/scope. Default: the calling "
                                "session's scope."},
            ],
        },
        {
            "name": "sources",
            "effect": "read",
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
                 "description": "For a relevant hit, the section it must answer in: exact or related. "
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


async def _h_query(args: dict, as_: str | None = None) -> dict:
    return await ARCHIVE.query(request.parse(args, as_))


def _log_get(args: dict, node: str) -> None:
    project, _, scope = str(args.get("context") or "").strip().partition("/")
    conn = store.connect()
    try:
        store.log_use(conn, {"project": project, "scope": scope,
                             "query": str(args.get("query") or "").strip(), "source": str(args["source"]),
                             "doc_id": str(args["id"]), "node": node})
    finally:
        conn.close()


async def _h_get(args: dict, as_: str | None = None) -> dict:
    if not privacy.from_peer(as_):  # the asking node logs a relayed get; the node serving it does not
        try:
            await asyncio.to_thread(_log_get, args, str(args.get("node") or "").strip() or request.LOCAL)
        except Exception as exc:  # noqa: BLE001 — feedback never fails the read
            log.warning("search: could not log a get: %s", exc)
    node = str(args.get("node") or "").strip()
    if node and node != request.LOCAL and not privacy.from_peer(as_) \
            and node_label(node) != node_label(config.node_name()):
        return await federation.fetch(ARCHIVE.peers, node, str(args["source"]), str(args["id"]))
    out = await ARCHIVE.get(str(args["source"]), str(args["id"]), request.hidden_for(as_))
    if out["document"].get("kind") == JOURNAL_KIND and args.get("_caller_pid"):
        task = asyncio.create_task(_note_read(str(args["id"]), args["_caller_pid"]))
        _NOTING.add(task)
        task.add_done_callback(_NOTING.discard)
    return out


#: The journal post kind whose get counts as a read for citations (sessions.md R15).
JOURNAL_KIND = "journal"
_NOTING: set[asyncio.Task] = set()


async def _note_read(post_id: str, caller_pid: Any) -> None:
    try:
        await ARCHIVE.gateway.call("journal", "note_read",
                                   {"post_id": post_id, "_caller_pid": caller_pid}, 10.0)
    except Exception as exc:  # noqa: BLE001 - a get never fails on the read record
        log.info("search: read of journal %s not recorded: %s", post_id, exc)


async def _h_sources(args: dict, as_: str | None = None) -> dict:
    return await ARCHIVE.sources_view(request.hidden_for(as_))


async def _h_status(args: dict, as_: str | None = None) -> dict:
    return await ARCHIVE.status_view(request.hidden_for(as_))


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
        raise ValueError(f"section: {section!r} is not exact or related")
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
    embeddings.warm()


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    await ServiceAdapter("search", API_MANIFEST, HANDLERS, on_start=_on_start).run()


if __name__ == "__main__":
    asyncio.run(main())
