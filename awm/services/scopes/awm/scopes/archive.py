"""Federation-wide journal search.

``archive_search`` runs this node's journal search and, in parallel, the same
local search on every peer in the book. Each peer answers through its own
gateway gate, so a peer that has not granted this node ``journals`` refuses and
shows up as a per-peer error. One slow, dead or refusing peer never fails the
search.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from awm import config
from awm.config.peertoken import node_label
from awm.scopes import channel

#: Budget for one peer, connect to reply.
PEER_TIMEOUT_S = 10.0
#: The peer verb every fan-out calls. It is the local-only search, never
#: ``scope_archive_search``, so a search cannot recurse through the federation.
PEER_VERB = "scope_fetch"
#: A peer reply larger than this is discarded unread.
MAX_REPLY_BYTES = 2 * 1024 * 1024
DEFAULT_LIMIT = 20
MAX_LIMIT = 100


async def _invoke_peer(peer: str, name: str, args: dict, *, timeout: float) -> Any:
    from awm.gatewayclient import invoke_peer
    return await invoke_peer(peer, name, args, timeout=timeout)


def _posts_of(reply: Any) -> list[dict]:
    """The ``posts`` of a ``scope_fetch`` reply, however the transport wrapped it."""
    if isinstance(reply, dict) and "posts" not in reply and "result" in reply:
        reply = reply["result"]
    if isinstance(reply, (str, bytes)):
        reply = json.loads(reply)
    if not isinstance(reply, dict):
        raise ValueError(f"unexpected reply of type {type(reply).__name__}")
    if not isinstance(reply.get("posts"), list):
        detail = reply.get("error") or reply.get("detail") or "reply has no posts"
        raise RuntimeError(str(detail))
    return [p for p in reply["posts"] if isinstance(p, dict) and p.get("kind") == "journal"]


def _check_size(reply: Any) -> None:
    size = len(reply) if isinstance(reply, (str, bytes)) else len(json.dumps(reply, default=str))
    if size > MAX_REPLY_BYTES:
        raise ValueError(f"reply of {size} bytes exceeds the {MAX_REPLY_BYTES} byte cap")


def _describe(exc: BaseException) -> str:
    return (f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__)[:300]


async def _search_peer(rec: dict, args: dict, limit: int) -> tuple[str, list[dict] | None, str | None]:
    """``(peer, hits, None)`` on success, ``(peer, None, error)`` on any failure."""
    name = rec["name"]
    try:
        reply = await asyncio.wait_for(
            _invoke_peer(name, PEER_VERB, args, timeout=PEER_TIMEOUT_S),
            timeout=PEER_TIMEOUT_S + 1.0)
        _check_size(reply)
        posts = _posts_of(reply)[:limit]
    except (asyncio.TimeoutError, TimeoutError):
        return name, None, f"timeout after {PEER_TIMEOUT_S:g}s"
    except Exception as exc:  # noqa: BLE001 — a peer's failure is reported, never raised
        return name, None, _describe(exc)
    swarm = rec.get("swarm")
    if rec.get("relation") != "domestic" and swarm == config.node_swarm():
        swarm = "?"  # the book fills a missing swarm with ours; a foreign peer never shares it
    for hit in posts:
        hit["origin_swarm"] = swarm
        hit["origin_node"] = name
    return name, posts, None


def _local_search(query: str, project: str | None, scope: str | None, limit: int) -> list[dict]:
    posts, _degraded, _engine = channel.search(
        query=query, project=project, scope=scope, kind="journal", limit=limit)
    swarm, node = config.node_swarm(), config.node_name()
    hits = [p.to_dict() for p in posts]
    for hit in hits:
        hit["origin_swarm"] = swarm
        hit["origin_node"] = node
    return hits


def _merge(ranked: list[list[dict]], limit: int) -> list[dict]:
    """Interleave by rank within each source, newest first among equal ranks.

    Scores from different nodes do not compare (each node ranks against its own
    corpus), so rank position is the only thing merged on.
    """
    entries = [(rank, hit) for hits in ranked for rank, hit in enumerate(hits)]
    entries.sort(key=lambda e: str(e[1].get("ts") or ""), reverse=True)
    entries.sort(key=lambda e: e[0])
    return [hit for _rank, hit in entries[:limit]]


def _peer_names(raw: Any) -> list[str] | None:
    if raw is None or raw == "":
        return None
    if isinstance(raw, str):
        raw = raw.split(",")
    return [str(n).strip() for n in raw if str(n).strip()]


async def archive_search(args: dict, as_: str | None = None) -> dict:
    """Journal hits for ``query`` from this node and every peer, tagged by origin.

    Returns ``{hits, peers}``; ``peers`` maps each peer asked to ``"ok"`` or
    ``{"error": ...}``. A call that itself arrives from a peer searches this
    node only: the caller fans out for itself, and a second fan-out here would
    relay our peers' journals to someone they never granted.
    """
    query = (args.get("query") or "").strip()
    if not query:
        raise ValueError("archive_search needs a query")
    project, scope = args.get("project"), args.get("scope")
    limit = max(1, min(int(args.get("limit") or DEFAULT_LIMIT), MAX_LIMIT))

    wanted = _peer_names(args.get("peers"))
    here = node_label(config.node_name())
    book = {r["name"]: r for r in config.list_records() if node_label(r["name"]) != here}
    peers: dict[str, Any] = {}
    records: list[dict] = []
    if channel.edge_origin(as_) is None:
        for name in (wanted if wanted is not None else sorted(book)):
            if name in book:
                records.append(book[name])
            else:
                peers[name] = {"error": "not in the peer book"}

    peer_args = {"query": query, "kind": "journal", "limit": limit}
    if project:
        peer_args["project"] = project
    if scope:
        peer_args["scope"] = scope

    local, *remote = await asyncio.gather(
        asyncio.to_thread(_local_search, query, project, scope, limit),
        *(_search_peer(rec, peer_args, limit) for rec in records))

    ranked = [local]
    for name, hits, error in remote:
        if error is None:
            peers[name] = "ok"
            ranked.append(hits)
        else:
            peers[name] = {"error": error}
    return {"hits": _merge(ranked, limit), "peers": peers}
