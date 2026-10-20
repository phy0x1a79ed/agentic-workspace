"""Hub adapter for the kb service: opt-in Cognee recall over the Zotero library, and the papers feed for `search`.

The recall itself runs in the kb server, a process from the `kb` project in its
own env (see `server`). This adapter supervises it, relays its routes as
`kb_<verb>` tools, and pages the library's papers to the search service as
`kb_export` (see `papers`).

**Who may call what.** Lifecycle and write verbs are operator-only, enforced by
`_operator_only` the way the trilium service does: the edge always stamps
`X-Awm-As`, so an absent identity means the call came from the host itself.

Run via `run.sh`:
    python -m awm.kb.hub_adapter
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import subprocess
import time
from typing import Any

from awm import config
from awm import gatewayclient
from awm.gatewayclient import ServiceAdapter, spawn_supervised

from awm.kb import client, fastlayer, graphrank, instances, papers, semantic, server

log = logging.getLogger("awm.kb.hub_adapter")

CHILD = server.CHILD
PAPERS = fastlayer.FastLayer(instances.PAPERS_BASE, instances.PAPERS_DELTA)
RANKS = graphrank.Ranks(instances.GRAPH)
MAX_RANKED = 1000
_tasks: set[asyncio.Task] = set()

API_MANIFEST: dict[str, Any] = {
    "description": (
        "Opt-in Cognee recall over the Zotero library (papers, with PDF full text where a PDF "
        "is stored). `search query` covers papers by default; use `kb recall` for mode=graph "
        "or mode=answer, which write one LLM answer from the top chunks."
    ),
    "functions": [
        {
            "name": "status",
            "effect": "read",
            "category": "kb",
            "tool": "kb_status",
            "description": (
                "Whether the kb server is up, how many papers it holds, and its ingest queue "
                "and last Zotero sync."
            ),
            "params": [],
        },
        {
            "name": "recall",
            "effect": "read",
            "category": "kb",
            "tool": "kb_recall",
            "description": (
                "Ranked paper hits for a query, each with its Zotero key, title, year and DOI. "
                "mode=hybrid (default) combines meaning and keywords and makes no LLM call. mode=answer writes "
                "one answer from the top chunks and costs about $0.001."
            ),
            "params": [
                {"name": "query", "type": "string", "required": True, "description": "What to find."},
                {"name": "mode", "type": "string",
                 "description": "hybrid (default), vector, lexical, graph or answer."},
                {"name": "limit", "type": "integer", "description": "Maximum hits, 1-100. Default 10."},
                {"name": "search_type", "type": "string",
                 "description": "For graph or answer mode: a Cognee search type. Default "
                                "RAG_COMPLETION for answer, GRAPH_COMPLETION for graph."},
            ],
            "timeout": 600,
        },
        {
            "name": "start",
            "effect": "write",
            "tool": "kb_start",
            "description": "Start the kb server if it is not running. Operator only.",
            "params": [],
            "timeout": 360,
        },
        {
            "name": "stop",
            "effect": "write",
            "tool": "kb_stop",
            "description": (
                "Stop the kb server and keep it down until `kb start`. Operator only."
            ),
            "params": [],
            "timeout": 60,
        },
        {
            "name": "restart",
            "effect": "write",
            "tool": "kb_restart",
            "description": "Stop then start the kb server. Operator only.",
            "params": [],
            "timeout": 420,
        },
        {
            "name": "logs",
            "effect": "read",
            "tool": "kb_logs",
            "description": "The tail of the kb server's log. Operator only.",
            "params": [{"name": "tail", "type": "integer", "description": "Lines from the end. Default 200."}],
        },
        {
            "name": "sync",
            "effect": "write",
            "tool": "kb_sync",
            "description": (
                "Re-read the Zotero library: through the API when ZOTERO_API_KEY is set, "
                "else from the vault's library.json. Runs in the background. Operator only."
            ),
            "params": [],
        },
        {
            "name": "snapshot",
            "effect": "write",
            "tool": "kb_snapshot",
            "description": (
                "Pause ingest, stop the server, copy its store to data/store in the kb "
                "checkout, `dvc add` it, and start again. Committing the pin is left to "
                "the caller. Operator only."
            ),
            "params": [],
            "timeout": 1800,
        },
        {
            "name": "query",
            "effect": "read",
            "category": "kb",
            "tool": "kb_query",
            "surfaces": ["cli", "http"],
            "description": (
                "Papers for the archive's live `kb` source, in search's sections: `exact` holds "
                "papers whose title, abstract or body holds the query literally, then papers "
                "matching its words. `related` holds papers near the query in meaning (the entry "
                "index's best papers per zone, merged with the keyword matches) and `graph` the "
                "papers one hop from the top hits and nearest entities, ordered by PageRank. Each "
                "hit quotes its passages with their zone, section and offsets. The answer says in "
                "`degraded` what it could not use, for example the encoder or the index. "
                "`search query` calls it; call that instead."
            ),
            "params": [
                {"name": "query", "type": "string", "required": True, "description": "What to find."},
                {"name": "limit", "type": "integer", "description": "Maximum hits per section. Default 10."},
                {"name": "passages", "type": "integer", "description": "Passages per hit. Default 1."},
                {"name": "kind", "type": "string",
                 "description": "Comma-separated kinds; papers answer only when it names paper."},
                {"name": "project", "type": "string", "description": "Papers have none, so any value answers nothing."},
                {"name": "scope", "type": "string", "description": "Papers have none, so any value answers nothing."},
                {"name": "after", "type": "string", "description": "Only papers from this ISO 8601 date on."},
                {"name": "before", "type": "string", "description": "Only papers before this ISO 8601 date."},
            ],
            "timeout": 30,
        },
        {
            "name": "graph",
            "effect": "read",
            "category": "kb",
            "tool": "kb_graph",
            "description": (
                "Run a read-only Cypher query on the knowledge graph (papers, authors, citations, posts, and "
                "the entities and relations extracted from papers). `near` binds meaning matches to "
                "parameters: {\"p\": {\"index\": \"entries\", \"text\": \"...\", \"k\": 3}} makes $near_p the "
                "keys of the 3 nearest entries' documents (index entries) or the node ids of the nearest "
                "entity groups (index nouns). Read `kb schema` first for the labels, the relation vocabulary "
                "and worked examples. Rows are not ranked by meaning: ORDER BY a rank property. Write "
                "clauses are refused. Domestic callers only."
            ),
            "params": [
                {"name": "cypher", "type": "string", "required": True, "description": "One read-only statement."},
                {"name": "near", "type": "object",
                 "description": "Variable name to {index: entries|nouns, text, k}; each binds $near_<name>."},
                {"name": "params", "type": "object", "description": "Other query parameters."},
                {"name": "limit", "type": "integer", "description": "Maximum rows, 1-5000. Default 200."},
                {"name": "timeout_s", "type": "number", "description": "Seconds before the query is cancelled, "
                                                                          "up to 60. Default 10."},
            ],
            "timeout": 90,
        },
        {
            "name": "schema",
            "effect": "read",
            "category": "kb",
            "tool": "kb_schema",
            "description": (
                "The graph's node labels with their properties, its relation tables, the relation-verb "
                "vocabulary of the extracted edges, how `near` binds, and worked Cypher examples."
            ),
            "params": [],
            "timeout": 15,
        },
        {
            "name": "related",
            "effect": "read",
            "category": "kb",
            "tool": "kb_related",
            "surfaces": ["cli", "http"],
            "description": (
                "Entry-index hits for a text, per zone (body, abstract, title, journal, precedent, note), each "
                "resolved to its source, document and span. `search query` calls it for the archive's own "
                "documents; domestic callers only."
            ),
            "params": [
                {"name": "text", "type": "string", "required": True, "description": "What to find."},
                {"name": "k", "type": "integer", "description": "Entries per zone, 1-200. Default 50."},
                {"name": "zones", "type": "array", "description": "Only these zones."},
                {"name": "sources", "type": "array", "description": "Only these sources."},
            ],
            "timeout": 15,
        },
        {
            "name": "rank",
            "effect": "read",
            "category": "kb",
            "tool": "kb_rank",
            "surfaces": ["cli", "http"],
            "description": (
                "PageRank scores for search hits, from the one graph over papers, authors, posts, scopes "
                "and projects. With a context, the scope's profile blends with its project's. "
                "`search query` calls it to order each section; domestic callers only."
            ),
            "params": [
                {"name": "hits", "type": "array", "required": True,
                 "description": "Hits as {source, id, project, scope}; source kb is a paper."},
                {"name": "context", "type": "string",
                 "description": "The calling scope as project/scope, or a project. Default: the global rank."},
            ],
            "timeout": 10,
        },
        {
            "name": "export",
            "effect": "read",
            "category": "kb",
            "tool": "kb_export",
            "surfaces": ["cli", "http"],
            "description": (
                "Page the Zotero library as search documents (id, title, text, ts, kind, url) for "
                "the archive's `kb` source. The last page also returns `ids`, every paper id. "
                "Local callers only."
            ),
            "params": [
                {"name": "cursor", "type": "string",
                 "description": "The previous page's next_cursor. Omit to start a pass."},
                {"name": "limit", "type": "integer",
                 "description": f"Documents per page, 1-{papers.PAGE_MAX}. Default {papers.PAGE_DEFAULT}."},
            ],
        },
    ],
    "emitters": [],
    "sessions": [],
}


def _operator_only(as_: str | None, verb: str) -> None:
    """Refuse a verb that arrived through an edge listener; see the trilium service's twin."""
    if as_ is not None:
        raise PermissionError(f"{verb} is an operator verb: run `awm kb {verb}` on the host")


async def _h_status(args: dict, as_: str | None = None) -> dict:
    if _foreign(as_):
        try:
            return {"kb": {"counts": (await client.status()).get("counts")}}
        except Exception:  # noqa: BLE001 — a foreign caller gets no detail
            return {"kb": {"error": "unavailable"}}
    out: dict[str, Any] = {"server": await asyncio.to_thread(CHILD.snapshot)}
    if out["server"]["listening"]:
        try:
            out["kb"] = await client.status()
        except Exception as exc:  # noqa: BLE001 — reported, not raised
            out["kb"] = {"error": repr(exc)[:300]}
    return out


#: The modes that rank rows from kb's own store and so honour `sources`. graph and answer run
#: cognee with access control off, where `datasets` does not scope, and answer spends LLM budget.
_PLAIN_MODES = ("hybrid", "vector", "lexical")


def _foreign(as_: str | None) -> bool:
    """A peer node outside this swarm. An unknown peer counts as foreign; a non-peer caller does not."""
    node = config.caller_peer(as_)
    return node is not None and config.peer_relation(node) != "domestic"


async def _h_recall(args: dict, as_: str | None = None) -> dict:
    body = {k: args[k] for k in ("query", "mode", "limit", "search_type") if args.get(k) is not None}
    body["sources"] = ["papers"]
    if _foreign(as_) and (body.get("mode", "hybrid") not in _PLAIN_MODES or "search_type" in body):
        raise PermissionError("a foreign peer may use only the hybrid, vector and lexical modes")
    return await client.recall(body)


async def _h_export(args: dict, as_: str | None = None) -> dict:
    if _foreign(as_):
        raise PermissionError("export is for the local search service")
    return await asyncio.to_thread(papers.export_page, instances.ZOTERO_LIBRARY,
                                   args.get("cursor"), args.get("limit"))


async def _h_query(args: dict, as_: str | None = None) -> dict:
    limit = max(1, min(int(args.get("limit") or 10), 100))
    per_hit = max(1, min(int(args.get("passages") or 1), 10))
    filters = fastlayer.Filters.from_args(args)
    text = str(args.get("query") or "")
    meaning = None if filters.blocked or not text.strip() else asyncio.ensure_future(_entries(text))
    found = await asyncio.to_thread(PAPERS.search, text, filters, limit, per_hit)
    if meaning is None:
        return {**found, "graph": []}
    served, why = await meaning
    degraded: dict[str, str] = {}
    related, graph = found["related"], []
    if served is None:
        degraded["semantic"] = f"meaning matches and graph unavailable, keyword only: {why}"
    else:
        exact_ids = {h["id"] for h in found["exact"]}
        related = await asyncio.to_thread(semantic.paper_hits, PAPERS, served["hits"], found["related"],
                                          exact_ids, filters, limit, per_hit)
        shown = exact_ids | {h["id"] for h in related}
        seeds = [h["id"] for h in [*found["exact"], *related]][:semantic.SEEDS]
        entities = list(dict.fromkeys(i for n in served.get("nouns") or [] for i in n["node_ids"]))
        if seeds or entities:
            near, errors = await semantic.neighbours([f"paper:{s}" for s in seeds], entities, limit)
            graph = await asyncio.to_thread(semantic.describe, PAPERS,
                                            {p: v for p, v in near.items() if p not in shown}, filters, limit,
                                            per_hit)
            if errors and not graph:
                degraded["graph"] = "; ".join(errors)
    out = {"exact": found["exact"], "related": related, "graph": graph}
    out["semantic"] = {"status": "ok" if served else "unavailable", "generation": (served or {}).get("generation")}
    if degraded:
        out["degraded"] = degraded
    return out


async def _entries(text: str) -> tuple[dict[str, Any] | None, str]:
    """The server's entry matches for ``text``, or None and why not: it must never fail the keyword answer."""
    try:
        return await semantic.entries_for(text), ""
    except Exception as exc:  # noqa: BLE001 — a missing encoder, index or server is ordinary
        return None, semantic.reason(exc)


async def _h_graph(args: dict, as_: str | None = None) -> dict:
    if _foreign(as_):
        raise PermissionError("graph is for domestic callers: a query can read this node's posts")
    body: dict[str, Any] = {"cypher": str(args.get("cypher") or "")}
    for key in ("near", "params", "limit", "timeout_s"):
        if args.get(key) is not None:
            body[key] = args[key]
    if not body["cypher"].strip():
        raise ValueError("graph needs a cypher query")
    return await client.graph(body, timeout=float(body.get("timeout_s") or 10) + 30)


async def _h_schema(args: dict, as_: str | None = None) -> dict:
    return await client.schema()


async def _h_related(args: dict, as_: str | None = None) -> dict:
    if _foreign(as_):
        raise PermissionError("related is for the local search service")
    body = {k: args[k] for k in ("text", "k", "zones", "sources") if args.get(k) is not None}
    return await client.related(body)


async def _h_rank(args: dict, as_: str | None = None) -> dict:
    if _foreign(as_):
        raise PermissionError("rank is for domestic callers")
    hits = args.get("hits") or []
    if not isinstance(hits, list) or len(hits) > MAX_RANKED or \
            not all(isinstance(h, dict) and h.get("id") for h in hits):
        raise ValueError(f"hits must be a list of at most {MAX_RANKED} {{source, id}} objects")
    return await asyncio.to_thread(RANKS.score, hits, str(args.get("context") or ""))


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


async def _h_sync(args: dict, as_: str | None = None) -> dict:
    _operator_only(as_, "sync")
    return await client.zotero_sync()


def _copy_store() -> dict[str, Any]:
    """Replace `data/store` with a copy of `live/`, then `dvc add` it."""
    dst = instances.SNAPSHOT_DIR
    tmp = dst.with_name(dst.name + ".tmp")
    shutil.rmtree(tmp, ignore_errors=True)
    shutil.copytree(instances.LIVE, tmp, ignore=shutil.ignore_patterns("*.sock", "fastembed", "logs"))
    if dst.exists():
        for d in [dst, *(p for p in dst.rglob("*") if p.is_dir())]:
            d.chmod(d.stat().st_mode | 0o200)
        shutil.rmtree(dst)
    tmp.rename(dst)
    size = sum(p.stat().st_size for p in dst.rglob("*") if p.is_file())
    out: dict[str, Any] = {"path": str(dst), "bytes": size}
    if (instances.CHECKOUT / ".dvc").is_dir():
        r = subprocess.run(["dvc", "add", str(dst.relative_to(instances.CHECKOUT))],
                           cwd=instances.CHECKOUT, capture_output=True, text=True, timeout=1200)
        out["dvc"] = {"rc": r.returncode, "output": (r.stdout + r.stderr)[-1000:]}
    else:
        out["dvc"] = {"rc": None, "output": "no .dvc in the checkout; copied without a pin"}
    return out


async def _h_snapshot(args: dict, as_: str | None = None) -> dict:
    _operator_only(as_, "snapshot")
    t = time.monotonic()
    quiesced = None
    if await asyncio.to_thread(instances.listening):
        quiesced = (await client.quiesce(900)).get("quiesced")
    await asyncio.to_thread(CHILD.stop, hold=True)
    try:
        copied = await asyncio.to_thread(_copy_store)
    finally:
        started = await asyncio.to_thread(CHILD.start)
        try:
            await client.resume()
        except Exception:  # noqa: BLE001 — a fresh server reads `paused` from its store
            log.warning("kb: resume after snapshot failed", exc_info=True)
    return {"quiesced": quiesced, **copied, "server": started.get("action"),
            "seconds": round(time.monotonic() - t, 1)}


HANDLERS = {
    "status": _h_status,
    "recall": _h_recall,
    "query": _h_query,
    "graph": _h_graph,
    "schema": _h_schema,
    "related": _h_related,
    "rank": _h_rank,
    "export": _h_export,
    "start": _h_start,
    "stop": _h_stop,
    "restart": _h_restart,
    "logs": _h_logs,
    "sync": _h_sync,
    "snapshot": _h_snapshot,
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
    reply = await gatewayclient.invoke_peer(peer, "zotero_items", args, timeout=60)
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
    config.load_env_file()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    await ServiceAdapter("kb", API_MANIFEST, HANDLERS, on_start=_on_start).run()


if __name__ == "__main__":
    asyncio.run(main())
