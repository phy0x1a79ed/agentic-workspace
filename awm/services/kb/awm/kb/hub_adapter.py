"""Hub adapter for the kb service: the papers `search` reads, and the kb server.

kb publishes no MCP tool. Every verb is internal: `search` calls query, library, get and related, and
an operator runs the lifecycle verbs with `awm kb <verb>` on the host.

The kb server, a process from the `kb` project in its own env (see `server`), encodes text and serves the
entry and noun indexes. This adapter supervises it and answers the paper verbs from papers.db itself.

**Who may call what.** Lifecycle and write verbs are operator-only, enforced by
`_operator_only` the way the trilium service does: the edge always stamps
`X-Awm-As`, so an absent identity means the call came from the host itself.

Run via `run.sh`:
    python -m awm.kb.hub_adapter
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

from awm import config
from awm import gatewayclient
from awm.gatewayclient import ServiceAdapter, spawn_supervised

from awm.kb import client, fastlayer, hops, instances, semantic, server

log = logging.getLogger("awm.kb.hub_adapter")

CHILD = server.CHILD
PAPERS = fastlayer.FastLayer(instances.PAPERS_BASE, instances.PAPERS_DELTA)
HOPS = hops.Hops(instances.HOPS)
_tasks: set[asyncio.Task] = set()

API_MANIFEST: dict[str, Any] = {
    "description": (
        "Papers: the kb and library sources `search query` reads, and the kb "
        "server that encodes queries. Call `search`; kb serves search only."
    ),
    "functions": [
        {
            "name": "status",
            "tool": "kb_status",
            "surfaces": ["cli", "http"],
            "description": (
                "Whether the kb server is up, and the state of its encoder and served indexes."
            ),
            "params": [],
        },
        {
            "name": "start",
            "tool": "kb_start",
            "surfaces": ["cli", "http"],
            "description": "Start the kb server if it is not running. Operator only.",
            "params": [],
            "timeout": 360,
        },
        {
            "name": "stop",
            "tool": "kb_stop",
            "surfaces": ["cli", "http"],
            "description": (
                "Stop the kb server and keep it down until `kb start`. Operator only."
            ),
            "params": [],
            "timeout": 60,
        },
        {
            "name": "restart",
            "tool": "kb_restart",
            "surfaces": ["cli", "http"],
            "description": "Stop then start the kb server. Operator only.",
            "params": [],
            "timeout": 420,
        },
        {
            "name": "logs",
            "tool": "kb_logs",
            "surfaces": ["cli", "http"],
            "description": "The tail of the kb server's log. Operator only.",
            "params": [{"name": "tail", "type": "integer", "description": "Lines from the end. Default 200."}],
        },
        {
            "name": "query",
            "tool": "kb_query",
            "surfaces": ["cli", "http"],
            "description": (
                "Papers for the archive's live `kb` source, in search's sections: `exact` holds "
                "papers whose title, abstract or body holds the query literally, then papers "
                "matching its words. `related` holds papers near the query in meaning (the entry "
                "index's best papers per zone, merged with the keyword matches). A hit carries its "
                "id, title, date and url and no passages; `kb get` reads the paper. The answer says in "
                "`degraded` what it could not use, for example the encoder or the index. "
                "`search query` calls it; call that instead."
            ),
            "params": [
                {"name": "query", "type": "string", "required": True, "description": "What to find."},
                {"name": "limit", "type": "integer", "description": "Maximum hits per section. Default 10."},
                {"name": "project", "type": "string", "description": "Papers have none, so any value answers nothing."},
                {"name": "scope", "type": "string", "description": "Papers have none, so any value answers nothing."},
                {"name": "after", "type": "string", "description": "Only papers from this ISO 8601 date on."},
                {"name": "before", "type": "string", "description": "Only papers before this ISO 8601 date."},
                {"name": "author", "type": "string",
                 "description": "Only papers with an author of this surname, matched loosely, so "
                                "'Huttenhauer' finds Huttenhower."},
            ],
            "timeout": 30,
        },
        {
            "name": "library",
            "tool": "kb_library",
            "surfaces": ["cli", "http"],
            "description": (
                "The Zotero library's papers for the archive's live `library` source, in search's sections, "
                "found the way `kb query` finds papers but among the library's alone. The same papers can "
                "also appear in the `kb` source, unboosted. `search query` calls it; call that instead."
            ),
            "params": [],  # kb_query's, set below
            "timeout": 30,
        },
        {
            "name": "get",
            "tool": "kb_get",
            "surfaces": ["cli", "http"],
            "description": (
                "One paper in full, for `search get` on the `kb` and `library` sources: its title, "
                "authors, date, journal, url, and its abstract and body as one text."
            ),
            "params": [{"name": "id", "type": "string", "required": True, "description": "The paper id."}],
            "timeout": 30,
        },
        {
            "name": "hop",
            "tool": "kb_hop",
            "surfaces": ["cli", "http"],
            "description": (
                "Papers one hop from the given papers: their authors' other "
                "papers (`authors`), the papers they cite (`cites`) or the papers citing them "
                "(`cited_by`), ranked by personalized PageRank from the first five, the first weighted "
                "most, and never by meaning; ties go to the newest."
            ),
            "params": [
                {"name": "ids", "type": "array", "required": True, "description": "Paper ids, best first."},
                {"name": "along", "type": "string", "description": "authors (default), cites or cited_by."},
                {"name": "after", "type": "string", "description": "Only papers published from this date on."},
                {"name": "before", "type": "string", "description": "Only papers published before this date."},
                {"name": "limit", "type": "integer", "description": "Maximum papers, 1-100. Default 20."},
            ],
            "timeout": 30,
        },
        {
            "name": "related",
            "tool": "kb_related",
            "surfaces": ["cli", "http"],
            "description": (
                "Entry-index hits for a text, per zone (body, abstract, title, journal, precedent, note), each "
                "resolved to its source, document and span. `search query` calls it for the archive's own "
                "documents; local callers only."
            ),
            "params": [
                {"name": "text", "type": "string", "required": True, "description": "What to find."},
                {"name": "k", "type": "integer", "description": "Entries per zone, 1-200. Default 50."},
                {"name": "zones", "type": "array", "description": "Only these zones."},
                {"name": "sources", "type": "array", "description": "Only these sources."},
            ],
            "timeout": 15,
        },
    ],
    "emitters": [],
    "sessions": [],
}


_FNS = {f["name"]: f for f in API_MANIFEST["functions"]}
_FNS["library"]["params"] = _FNS["query"]["params"]


def _operator_only(as_: str | None, verb: str) -> None:
    """Refuse a verb that arrived through an edge listener; see the trilium service's twin."""
    if as_ is not None:
        raise PermissionError(f"{verb} is an operator verb: run `awm kb {verb}` on the host")


async def _h_status(args: dict, as_: str | None = None) -> dict:
    out: dict[str, Any] = {"server": await asyncio.to_thread(CHILD.snapshot)}
    if out["server"]["listening"]:
        try:
            out["kb"] = await client.status()
        except Exception as exc:  # noqa: BLE001 — reported, not raised
            out["kb"] = {"error": repr(exc)[:300]}
    return out


#: kb_query and kb_library share one computation for this long: search asks both for one question at once.
SHARED_S = 5.0
_answers: dict[str, tuple[float, "asyncio.Future[dict[str, Any]]"]] = {}


def _key(args: dict) -> str:
    return json.dumps({k: v for k, v in sorted(args.items()) if not k.startswith("_")}, sort_keys=True, default=str)


async def _shared(args: dict) -> dict[str, Any]:
    """Both sources' answers to ``args``: computed once, and handed to every caller within :data:`SHARED_S`."""
    now = time.monotonic()
    for k in [k for k, (at, _) in _answers.items() if now - at > SHARED_S]:
        del _answers[k]
    key = _key(args)
    if key not in _answers:
        _answers[key] = (now, asyncio.ensure_future(_answer(args)))
    try:
        return await asyncio.shield(_answers[key][1])
    except BaseException:
        _answers.pop(key, None)
        raise


async def _h_query(args: dict, as_: str | None = None) -> dict:
    return (await _shared(args))["kb"]


async def _h_library(args: dict, as_: str | None = None) -> dict:
    return (await _shared(args))["library"]


def _library_filters(filters: fastlayer.Filters) -> fastlayer.Filters:
    mine = PAPERS.library()
    papers_ = mine if filters.papers is None else mine & filters.papers
    return fastlayer.Filters(filters.after, filters.before, filters.blocked or not papers_, papers_)


async def _answer(args: dict) -> dict[str, Any]:
    limit = max(1, min(int(args.get("limit") or 10), 100))
    filters = fastlayer.Filters.from_args(args)
    author = str(args.get("author") or "").strip()
    if author and not filters.blocked:
        filters.papers = await asyncio.to_thread(HOPS.by_author, author)
        if filters.papers is None:
            raise ValueError("the author filter needs kb's hop arrays, which this node does not have")
        filters.blocked = not filters.papers
    mine = await asyncio.to_thread(_library_filters, filters)
    text = str(args.get("query") or "")
    meaning = None if filters.blocked or not text.strip() else asyncio.ensure_future(_entries(text))
    found, theirs = await asyncio.gather(asyncio.to_thread(PAPERS.search, text, filters, limit),
                                         asyncio.to_thread(PAPERS.search_library, text, mine, limit))
    if meaning is None:
        return {"kb": found, "library": theirs}
    served, why = await meaning
    degraded: dict[str, str] = {}
    related, own = found["related"], theirs["related"]
    if served is None:
        degraded["semantic"] = f"meaning matches unavailable, keyword only: {why}"
    else:
        exact_ids = {h["id"] for h in found["exact"]}
        related = await asyncio.to_thread(semantic.paper_hits, PAPERS, served["hits"], found["related"],
                                          exact_ids, filters, limit)
        own = await asyncio.to_thread(semantic.paper_hits, PAPERS, served["hits"], theirs["related"],
                                      {h["id"] for h in theirs["exact"]}, mine, limit)
    status = {"status": "ok" if served else "unavailable", "generation": (served or {}).get("generation")}
    out = {"exact": found["exact"], "related": related, "semantic": status}
    lib = {"exact": theirs["exact"], "related": own, "semantic": status}
    if degraded:
        out["degraded"] = lib["degraded"] = degraded
    return {"kb": out, "library": lib}


async def _h_get(args: dict, as_: str | None = None) -> dict:
    paper_id = str(args.get("id") or "").strip()
    paper = await asyncio.to_thread(PAPERS.paper, paper_id) if paper_id else None
    if paper is None:
        raise ValueError(f"no paper {paper_id!r}")
    return {"document": paper}


async def _h_hop(args: dict, as_: str | None = None) -> dict:
    ids = args.get("ids") or []
    if isinstance(ids, str):
        ids = [i.strip() for i in ids.split(",") if i.strip()]
    if not ids:
        raise ValueError("hop needs paper ids")
    along = str(args.get("along") or "authors")
    limit = max(1, min(int(args.get("limit") or 20), 100))
    got = await asyncio.to_thread(HOPS.hop, [str(i) for i in ids], along, str(args.get("after") or ""),
                                  str(args.get("before") or ""), limit)
    hits = await asyncio.to_thread(semantic.describe, PAPERS, {p: (along, s, "") for p, s in got},
                                   fastlayer.Filters(), limit)
    return {"hits": hits, "along": along}


async def _entries(text: str) -> tuple[dict[str, Any] | None, str]:
    """The server's entry matches for ``text``, or None and why not: it must never fail the keyword answer."""
    try:
        return await semantic.entries_for(text), ""
    except Exception as exc:  # noqa: BLE001 — a missing encoder, index or server is ordinary
        return None, semantic.reason(exc)


async def _h_related(args: dict, as_: str | None = None) -> dict:
    if config.caller_peer(as_) is not None:
        raise PermissionError("related is for the local search service")
    body = {k: args[k] for k in ("text", "k", "zones", "sources") if args.get(k) is not None}
    return await client.related(body)


async def _h_start(args: dict, as_: str | None = None) -> dict:
    _operator_only(as_, "start")
    return await asyncio.to_thread(CHILD.start)


async def _h_stop(args: dict, as_: str | None = None) -> dict:
    _operator_only(as_, "stop")
    return await asyncio.to_thread(CHILD.stop, hold=True)


async def _h_restart(args: dict, as_: str | None = None) -> dict:
    _operator_only(as_, "restart")
    return await asyncio.to_thread(CHILD.restart)


async def _h_logs(args: dict, as_: str | None = None) -> dict:
    _operator_only(as_, "logs")
    tail = int(args.get("tail") or 200)
    return {"tail": tail, "log": await asyncio.to_thread(CHILD.logs, tail)}


HANDLERS = {
    "status": _h_status,
    "query": _h_query,
    "library": _h_library,
    "get": _h_get,
    "hop": _h_hop,
    "related": _h_related,
    "start": _h_start,
    "stop": _h_stop,
    "restart": _h_restart,
    "logs": _h_logs,
}


async def _health_loop() -> None:
    """Respawn the server if it died. Never exits."""
    while True:
        try:
            await asyncio.sleep(instances.HEALTH_INTERVAL_S)
            res = await asyncio.to_thread(CHILD.reconcile)
            if res.get("action") == "respawned":
                log.warning("kb: server respawned (previous exit %s)", res.get("previous_exit"))
            elif res.get("action") == "respawn-failed":
                log.error("kb: respawn failed: %s", res.get("error"))
        except Exception:  # noqa: BLE001 — never let the loop die
            log.exception("kb: supervision pass failed")


async def _zotero_page(args: dict[str, Any]) -> dict[str, Any]:
    peer = instances.ZOTERO_PEER
    if peer in ("", "local", config.node_name()):
        return await gatewayclient.call("zotero", "items", args, timeout=60)
    reply = await gatewayclient.call("zotero", "items", args, peer=peer, timeout=60)
    return reply["result"] if isinstance(reply, dict) and "items" not in reply and "result" in reply else reply


async def zotero_poll() -> dict[str, Any]:
    """Take every library item that moved since the delta's cursor; store the cursor once all are in."""
    since = await asyncio.to_thread(PAPERS.cursor)
    totals = {"mapped": 0, "held": 0}
    offset: int | None = 0
    while offset is not None:
        page = await _zotero_page({"since": since, "offset": offset, "limit": 200})
        offset = page.get("next")
        done = await asyncio.to_thread(PAPERS.take_items, page.get("items") or [],
                                       page.get("versions") if offset is None else None)
        totals = {k: totals[k] + done[k] for k in totals}
    return totals


async def _papers_loop() -> None:
    """Keep the delta current: poll Zotero, and re-map held items whenever a new base ships. Never exits."""
    shipped = None
    while True:
        try:
            stamp = instances.PAPERS_BASE.stat().st_mtime if instances.PAPERS_BASE.exists() else None
            if stamp != shipped:
                log.info("kb: papers base %s: %s", "shipped" if stamp else "absent",
                         await asyncio.to_thread(PAPERS.remap))
                shipped = stamp
            moved = await zotero_poll()
            if any(moved.values()):
                log.info("kb: zotero items taken: %s", moved)
            await asyncio.to_thread(PAPERS.library_copy)
        except Exception as exc:  # noqa: BLE001 — mira away is ordinary; retry next round
            log.warning("kb: papers delta round failed: %s", repr(exc)[:300])
        await asyncio.sleep(instances.ZOTERO_POLL_S)


async def _initial_start() -> None:
    try:
        res = await asyncio.to_thread(CHILD.start)
        log.info("kb: %s pid=%s listening=%s", res.get("action"), res.get("pid"), res.get("listening"))
    except Exception:  # noqa: BLE001
        log.exception("kb: initial start failed; the loop will retry")


async def _on_start() -> None:
    """Start the server in the background and the loops beside it. Nothing here blocks or raises."""
    spawn_supervised("kb:papers", _papers_loop)
    if not instances.has_server():
        log.warning("kb: no kb server in %s — set KB_CHECKOUT; registering anyway", instances.CHECKOUT)
        return
    _tasks.add(asyncio.get_running_loop().create_task(_initial_start()))
    spawn_supervised("kb:health", _health_loop)


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    await ServiceAdapter("kb", API_MANIFEST, HANDLERS, on_start=_on_start).run()


if __name__ == "__main__":
    asyncio.run(main())
