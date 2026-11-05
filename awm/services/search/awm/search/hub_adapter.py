"""Hub adapter for the search service: one search over the sources a caller names, on any node.

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
from awm.gatewayclient import ServiceAdapter, spawn_supervised
from awm.persistence import embeddings

from awm.search import federation, query as request, store
from awm.search.archive import Archive

log = logging.getLogger("awm.search.hub_adapter")

_CLI_HTTP = ["cli", "http"]

QUERY_DESCRIPTION = """\
search query — rank items from named sources.
  query: string | {"text"?: T, "<source>": F, ...}    max: hits per source per section, default 10

<source>: journals, kb or transcripts, with @node for another node. `sources` lists their fields.
F: MongoDB filter on the source's fields: $and $or $nor $eq $ne $gt $gte $lt $lte $in $nin $all.
   {} matches all. * in a value globs. A scope covers its nested scopes. ts is ISO-8601.
T: string | {"$contains": s} | {"$about": s} | {"$and"|"$or": [...]}, shared by all sources.
   $contains: case-insensitive substring, as grep. Nests under $and $or $nor.
   $about: rank by meaning. At most one, directly under the top $and or $or.
   Under $and or alone it only ranks the matches. Under $or it also adds the nearest others, as `related`.
Short forms: query s = {"text": s}. No source = this node's journals. T s = {"$or": [{"$about": s}, {"$contains": s}]}.
Returns {source: {exact, related}}. A hit is {id, title, ts}. Open one with `get`.
"""

API_MANIFEST: dict[str, Any] = {
    "functions": [
        {
            "name": "query",
            "tool": "search_query",
            "timeout": 60,
            "description": QUERY_DESCRIPTION,
            "params": [
                {"name": "query", "type": ["string", "object"], "required": True,
                 "description": "A string, or an object {\"text\"?: T, \"<source>\": F, ...}."},
                {"name": "max", "type": "integer",
                 "description": "Hits per source per section, 1-100. Default 10."},
            ],
        },
        {
            "name": "get",
            "tool": "search_get",
            "description": (
                "One document in full: title, text, project, scope, kind, tags, ts and url; a "
                "paper adds its authors and journal. Take `source` and `id` from a hit. For a hit from "
                "another node, set `node` to the node of its source. Pass the `query` the hit "
                "answered: each get is logged with the caller's scope as feedback on search."
            ),
            "params": [
                {"name": "source", "type": "string", "required": True, "description": "The hit's source."},
                {"name": "id", "type": "string", "required": True, "description": "The hit's id."},
                {"name": "node", "type": "string",
                 "description": "The node that holds the document. Default: this node."},
                {"name": "query", "type": "string", "description": "The query whose hit this is."},
            ],
        },
        {
            "name": "sources",
            "tool": "search_sources",
            "description": (
                "Every source the archive holds, with its kind, node, fields (multi-valued ones "
                "marked), document count, last successful sync, whether it is frozen (its service "
                "is not running, so its last documents are served) and its last error; and the "
                "archive's health: whether semantic ranking is available, the registry's last "
                "read, running sweeps and push subscriptions."
            ),
            "params": [],
        },
        {
            "name": "status",
            "tool": "search_status",
            "surfaces": _CLI_HTTP,
            "description": (
                "The archive's health: documents per source, whether semantic ranking is "
                "available, the registry's last read, running sweeps and push subscriptions."
            ),
            "params": [],
        },
        {
            "name": "reindex",
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
            "tool": "search_judge",
            "surfaces": _CLI_HTTP,
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
    return await ARCHIVE.query(request.parse(args, as_), _relayer(as_))


def _relayer(as_: str | None) -> str | None:
    """The node that relayed a call, or ``None`` for a call made on this node."""
    return (config.caller_peer(as_) or "peer") if privacy.from_peer(as_) else None


def _log_get(args: dict, node: str) -> None:
    project, _, scope = str(args.get("_caller_scope") or "").strip().partition("/")
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
            await asyncio.to_thread(_log_get, args, str(args.get("node") or "").strip() or THIS_NODE)
        except Exception as exc:  # noqa: BLE001 — feedback never fails the read
            log.warning("search: could not log a get: %s", exc)
    node = str(args.get("node") or "").strip()
    await ARCHIVE.refuse_clashed(_relayer(as_))
    if not request.is_here(node) and not privacy.from_peer(as_):
        await ARCHIVE.refuse_clashed(node)
        return await federation.fetch(ARCHIVE.peers, node, str(args["source"]), str(args["id"]))
    out = await ARCHIVE.get(str(args["source"]), str(args["id"]), request.hidden_for(as_))
    if JOURNAL_TAG in out["document"].get("tags", ()) and args.get("_caller_pid"):
        task = asyncio.create_task(_note_read(str(args["id"]), args["_caller_pid"]))
        _NOTING.add(task)
        task.add_done_callback(_NOTING.discard)
    return out


#: The usage log's name for this node.
THIS_NODE = "local"
#: The journal tag whose post's get counts as a read for citations (sessions.md R15).
JOURNAL_TAG = "journal"
_NOTING: set[asyncio.Task] = set()


async def _note_read(post_id: str, caller_pid: Any) -> None:
    try:
        await ARCHIVE.gateway.call("journal", "note_read",
                                   {"post_id": post_id, "_caller_pid": caller_pid}, 10.0)
    except Exception as exc:  # noqa: BLE001 - a get never fails on the read record
        log.info("search: read of journal %s not recorded: %s", post_id, exc)


RETIRED = frozenset({"precedence", "notes"})


async def _h_sources(args: dict, as_: str | None = None) -> dict:
    hidden = request.hidden_for(as_)
    listed = (await ARCHIVE.sources_view(hidden))["sources"]
    health = await ARCHIVE.status_view(hidden)
    node = config.node_name()
    out = []
    for row in listed:
        if row["name"] in RETIRED:
            continue
        fields = request.FIELDS.get(row["name"], {})
        out.append({**row, "kind": row["name"], "node": node,
                    "fields": {f: ("multi" if multi else "single") for f, multi in fields.items()}})
    return {**{k: v for k, v in health.items() if k != "sources"}, "sources": out}


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
