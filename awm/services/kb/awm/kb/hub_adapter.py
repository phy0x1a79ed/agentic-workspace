"""Hub adapter for the kb service: one semantic recall over this node's journals and the Zotero library.

The search itself runs in the kb server, a process from the `kb` project in its
own env (see `server`). This adapter supervises it, feeds it this node's scope
posts (see `feed`), and relays its routes as `kb_<verb>` tools.

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
from awm.gatewayclient import ServiceAdapter, spawn_supervised

from awm.kb import client, instances, server
from awm.kb.feed import FEED

log = logging.getLogger("awm.kb.hub_adapter")

CHILD = server.CHILD
_tasks: set[asyncio.Task] = set()

API_MANIFEST: dict[str, Any] = {
    "description": (
        "Semantic recall over this node's scope posts and the Zotero library (papers, with "
        "PDF full text where a PDF is stored). `scope_fetch(query=...)` already routes to "
        "kb once kb holds every post, so use `kb recall` for papers, or for "
        "mode=answer, which joins a journal to a paper in one LLM-written answer."
    ),
    "functions": [
        {
            "name": "status",
            "effect": "read",
            "category": "kb",
            "tool": "kb_status",
            "description": (
                "Whether the kb server is up, how many posts and papers it holds, its ingest "
                "queue, whether it holds every post (`posts_complete`, the gate scope_fetch "
                "uses), and the feed's last sweep."
            ),
            "params": [],
        },
        {
            "name": "recall",
            "effect": "read",
            "category": "kb",
            "tool": "kb_recall",
            "description": (
                "Ranked hits for a query, each with its source: a post id with project and "
                "scope, or a Zotero key with title, year and DOI. mode=hybrid (default) "
                "combines meaning and keywords and makes no LLM call. mode=answer writes "
                "one answer from the top chunks and costs about $0.001."
            ),
            "params": [
                {"name": "query", "type": "string", "required": True, "description": "What to find."},
                {"name": "mode", "type": "string",
                 "description": "hybrid (default), vector, lexical, graph or answer."},
                {"name": "sources", "type": "array", "items": {"type": "string"},
                 "description": "posts and/or papers. Default both."},
                {"name": "project", "type": "string", "description": "Only posts of this project."},
                {"name": "scope", "type": "string", "description": "Only posts of this scope."},
                {"name": "kind", "type": "string", "description": "Only posts of this kind."},
                {"name": "limit", "type": "integer", "description": "Maximum hits, 1-100. Default 10."},
                {"name": "require_complete", "type": "boolean",
                 "description": "Refuse unless kb holds every post. Default false."},
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
                "Stop the kb server and keep it down until `kb start`. scope_fetch falls "
                "back to its local index meanwhile. Operator only."
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
            "name": "sweep",
            "effect": "write",
            "tool": "kb_sweep",
            "description": (
                "Send kb every post on this node now, and have it forget deleted ones. "
                "Runs hourly on its own. Operator only."
            ),
            "params": [],
            "timeout": 600,
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
    out: dict[str, Any] = {"server": await asyncio.to_thread(CHILD.snapshot), "feed": FEED.snapshot()}
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


def _posts_readable(as_: str | None) -> bool:
    """Whether this caller may read scope-post text through kb.

    Posts are journal content, so a foreign peer needs the `journals` grant on top of `kb`.
    """
    if not _foreign(as_):
        return True
    return "journals" in ((config.peer_record(config.caller_peer(as_)) or {}).get("grants") or [])


async def _h_recall(args: dict, as_: str | None = None) -> dict:
    body = {k: args[k] for k in ("query", "mode", "sources", "project", "scope", "kind", "limit",
                                 "require_complete", "search_type") if args.get(k) is not None}
    if _foreign(as_) and (body.get("mode", "hybrid") not in _PLAIN_MODES or "search_type" in body):
        raise PermissionError("a foreign peer may use only the hybrid, vector and lexical modes")
    if not _posts_readable(as_):
        body["sources"] = [s for s in (body.get("sources") or ["posts", "papers"]) if s != "posts"]
        if not body["sources"]:
            return {"hits": [], "note": "posts need the journals grant"}
    return await client.recall(body)


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


async def _h_sweep(args: dict, as_: str | None = None) -> dict:
    _operator_only(as_, "sweep")
    return await FEED.sweep()


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
    "start": _h_start,
    "stop": _h_stop,
    "restart": _h_restart,
    "logs": _h_logs,
    "sweep": _h_sweep,
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


async def _initial_start() -> None:
    try:
        res = await asyncio.to_thread(CHILD.start)
        log.info("kb: %s pid=%s listening=%s", res.get("action"), res.get("pid"), res.get("listening"))
    except Exception:  # noqa: BLE001
        log.exception("kb: initial start failed; the loop will retry")


async def _on_start() -> None:
    """Start the server in the background and the loops beside it. Nothing here blocks or raises."""
    if not instances.has_server():
        log.warning("kb: no kb server in %s — set KB_CHECKOUT; registering anyway", instances.CHECKOUT)
        return
    _tasks.add(asyncio.get_running_loop().create_task(_initial_start()))
    spawn_supervised("kb:health", _health_loop)
    spawn_supervised("kb:feed", FEED.subscribe)
    spawn_supervised("kb:sweep", FEED.loop)


async def main() -> None:
    config.load_env_file()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    await ServiceAdapter("kb", API_MANIFEST, HANDLERS, on_start=_on_start).run()


if __name__ == "__main__":
    asyncio.run(main())
