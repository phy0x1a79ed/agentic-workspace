"""Asking other nodes: the named peers, or every domestic node, the same question, each answer apart.

Every peer answers through its own gateway gate, which keeps only the sources
that peer granted this node, so what a peer returns is already narrowed to its
grants. A station runs no search and is never asked. A peer that fails, refuses
or runs an older release shows as a per-peer status and never fails the search.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any

from awm import config
from awm.config.peertoken import node_label

#: Budget for one peer, connect to reply.
PEER_TIMEOUT_S = 10.0
#: The verb every fan-out calls, with `peers=false`, so a search cannot recurse through the federation.
PEER_TOOL = "search_query"
GET_TOOL = "search_get"
#: A reply larger than this is discarded unread.
MAX_REPLY_BYTES = 2 * 1024 * 1024
_TEXT_KEYS = ("source", "id", "title", "snippet", "ts", "project", "scope", "kind", "url")


class Peers:
    """The peer book and the call to a peer; tests stand in for both."""

    def records(self) -> list[dict[str, Any]]:
        here = node_label(config.node_name())
        return [r for r in config.list_records() if node_label(r["name"]) != here]

    async def invoke(self, peer: str, name: str, args: dict[str, Any], timeout: float) -> Any:
        from awm.gatewayclient import invoke_peer
        return await invoke_peer(peer, name, args, timeout=timeout)


@dataclass
class PeerResult:
    name: str
    hits: list[dict[str, Any]]
    status: dict[str, Any]


def _describe(exc: BaseException) -> str:
    return (f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__)[:300]


def _no_tool(exc: BaseException) -> bool:
    from awm.gatewayclient import GatewayCallError
    return isinstance(exc, GatewayCallError) and (exc.status == 404 or "Unknown tool" in exc.body)


def _reply(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict) and "hits" not in raw and "result" in raw:
        raw = raw["result"]
    size = len(raw) if isinstance(raw, (str, bytes)) else len(json.dumps(raw, default=str))
    if size > MAX_REPLY_BYTES:
        raise ValueError(f"reply of {size} bytes exceeds the {MAX_REPLY_BYTES} byte cap")
    if isinstance(raw, (str, bytes)):
        raw = json.loads(raw)
    if not isinstance(raw, dict) or not isinstance(raw.get("hits"), list):
        detail = raw.get("error") if isinstance(raw, dict) else None
        raise RuntimeError(str(detail or "reply has no hits"))
    return raw


def _origin(record: dict[str, Any]) -> dict[str, str]:
    swarm = record.get("swarm")
    if record.get("relation") != "domestic" and swarm == config.node_swarm():
        swarm = "?"  # the book fills a missing swarm with ours; a foreign peer never shares it
    return {"node": record["name"], "swarm": swarm}


def _hit(raw: Any, origin: dict[str, str]) -> dict[str, Any] | None:
    """A peer's hit cut down to the shared keys, so a peer cannot inject others or claim our origin."""
    if not isinstance(raw, dict) or not raw.get("source") or not raw.get("id"):
        return None
    hit = {k: str(raw[k])[:2000] for k in _TEXT_KEYS if raw.get(k) not in (None, "")}
    return {"title": "", "snippet": "", "ts": "", **hit, "score": 0.0, "origin": origin}


async def _ask_one(peers: Peers, record: dict[str, Any], args: dict[str, Any], limit: int) -> PeerResult:
    name = record["name"]
    if record.get("role") == "station":
        return PeerResult(name, [], {"status": "station"})
    try:
        raw = await asyncio.wait_for(peers.invoke(name, PEER_TOOL, args, PEER_TIMEOUT_S),
                                     timeout=PEER_TIMEOUT_S + 1.0)
        reply = _reply(raw)
    except (asyncio.TimeoutError, TimeoutError):
        return PeerResult(name, [], {"status": "error", "error": f"timeout after {PEER_TIMEOUT_S:g}s"})
    except Exception as exc:  # noqa: BLE001 — a peer's failure is reported, never raised
        if _no_tool(exc) and record.get("relation") == "domestic":
            return PeerResult(name, [], {"status": "no-search", "detail": "its release has no search tool"})
        if _no_tool(exc):
            return PeerResult(name, [], {"status": "unsupported",
                                         "detail": "no search tool there, or no source granted to this node"})
        return PeerResult(name, [], {"status": "error", "error": _describe(exc)})
    origin = _origin(record)
    hits = [h for h in (_hit(x, origin) for x in reply["hits"]) if h][:limit]
    sources = reply.get("sources")
    return PeerResult(name, hits, {"status": "ok", "hits": len(hits),
                                   "sources": sorted(sources) if isinstance(sources, dict) else []})


async def ask(peers: Peers, wanted: list[str] | str, args: dict[str, Any],
              limit: int) -> list[PeerResult]:
    """One result per peer asked. ``wanted`` names the peers, or is ``"fleet"`` for every domestic one."""
    book = {r["name"]: r for r in peers.records()}
    if isinstance(wanted, str):
        names = sorted(n for n, r in book.items() if r.get("relation", "domestic") == "domestic")
    else:
        names = wanted
    unknown = [PeerResult(n, [], {"status": "error", "error": "not in the peer book"})
               for n in names if n not in book]
    asked = await asyncio.gather(*(_ask_one(peers, book[n], args, limit) for n in names if n in book))
    return [*asked, *unknown]


async def fetch(peers: Peers, name: str, source: str, doc_id: str) -> dict[str, Any]:
    """One document from a peer's archive, stamped with the peer as its origin."""
    record = next((r for r in peers.records() if r["name"] == name), None)
    if record is None:
        raise ValueError(f"{name!r} is not in the peer book")
    raw = await asyncio.wait_for(peers.invoke(name, GET_TOOL, {"source": source, "id": doc_id}, PEER_TIMEOUT_S),
                                 timeout=PEER_TIMEOUT_S + 1.0)
    if isinstance(raw, dict) and "document" not in raw and "result" in raw:
        raw = raw["result"]
    if isinstance(raw, (str, bytes)):
        raw = json.loads(raw)
    doc = raw.get("document") if isinstance(raw, dict) else None
    if not isinstance(doc, dict):
        raise RuntimeError(str((raw or {}).get("error") if isinstance(raw, dict) else "") or "reply has no document")
    return {"document": {**doc, "source": source, "id": doc_id, "origin": _origin(record)}}
