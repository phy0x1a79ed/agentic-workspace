"""Asking another node's search for the sources named on it, each answer apart.

A station runs no search and is never asked, nor is a node with no edge from
this one, nor one that an open tag clash stands between. A node that fails,
refuses or runs an older release shows as a status and never fails the search.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any

from awm.config import edges
from awm.search import hits as hitshape

#: Budget for one peer, connect to reply.
PEER_TIMEOUT_S = 10.0
#: The verb a node's sources are asked with. A relayed call answers from that node alone.
PEER_TOOL = "search_query"
GET_TOOL = "search_get"
#: A reply larger than this is discarded unread.
MAX_REPLY_BYTES = 2 * 1024 * 1024


class Peers:
    """The nodes this node holds an edge to, and the call to one; tests stand in for both."""

    def records(self) -> list[dict[str, Any]]:
        return [{"name": n} for n in edges.reachable_from(edges.here())]

    async def invoke(self, peer: str, name: str, args: dict[str, Any], timeout: float) -> Any:
        from awm import gatewayclient
        tool, verb = name.split("_", 1)
        return await gatewayclient.call(tool, verb, args, peer=peer, timeout=timeout)


@dataclass
class PeerResult:
    name: str
    status: dict[str, Any]
    found: dict[str, list[dict[str, Any]]] = field(default_factory=dict)


def describe_error(exc: BaseException) -> str:
    """An exception as one line of at most 300 characters, for a source's status."""
    return (f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__)[:300]


def _no_tool(exc: BaseException) -> bool:
    from awm.gatewayclient import GatewayCallError
    return isinstance(exc, GatewayCallError) and (exc.status == 404 or "Unknown tool" in exc.body)


def _reply(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict) and "result" in raw and not any(k in raw for k in ("hits", *hitshape.SECTIONS)):
        raw = raw["result"]
    size = len(raw) if isinstance(raw, (str, bytes)) else len(json.dumps(raw, default=str))
    if size > MAX_REPLY_BYTES:
        raise ValueError(f"reply of {size} bytes exceeds the {MAX_REPLY_BYTES} byte cap")
    if isinstance(raw, (str, bytes)):
        raw = json.loads(raw)
    if not isinstance(raw, dict) or not any(isinstance(raw.get(k), (list, dict))
                                            for k in ("hits", *hitshape.SECTIONS)):
        detail = raw.get("error") if isinstance(raw, dict) else None
        raise RuntimeError(str(detail or "reply has no hits"))
    return raw


def _origin(record: dict[str, Any]) -> dict[str, str]:
    return {"node": record["name"]}


async def _ask_one(peers: Peers, record: dict[str, Any], args: dict[str, Any], limit: int) -> PeerResult:
    name = record["name"]
    if edges.is_station(name):
        return PeerResult(name, {"status": "station"})
    try:
        raw = await asyncio.wait_for(peers.invoke(name, PEER_TOOL, args, PEER_TIMEOUT_S),
                                     timeout=PEER_TIMEOUT_S + 1.0)
        reply = _reply(raw)
    except (asyncio.TimeoutError, TimeoutError):
        return PeerResult(name, {"status": "error", "error": f"timeout after {PEER_TIMEOUT_S:g}s"})
    except Exception as exc:  # noqa: BLE001 — a peer's failure is reported, never raised
        if _no_tool(exc):
            return PeerResult(name, {"status": "no-search", "detail": "its release has no search tool"})
        return PeerResult(name, {"status": "error", "error": describe_error(exc)})
    found = hitshape.sections(reply, _origin(record), limit)
    sources = reply.get("sources")
    return PeerResult(name, {"status": "ok", "hits": hitshape.count(found),
                             "sources": sources if isinstance(sources, dict) else {}}, found)


async def ask_home(peers: Peers, home: str, args: dict[str, Any], limit: int,
                   clashed: bool = False) -> PeerResult:
    """The sections of the sources ``args`` names, asked of the node that holds them."""
    record = next((r for r in peers.records() if edges.label(r["name"]) == edges.label(home)), None)
    if record is None:
        return PeerResult(home, {"status": "error", "error": f"no edge from {edges.here()} to {home}"})
    if clashed:
        return PeerResult(home, {"status": "refused", "error": CLASH_REFUSAL})
    return await _ask_one(peers, record, args, limit)


CLASH_REFUSAL = "an open tag clash stands between this node and it; searches wait until an agent renames the tag"


async def fetch(peers: Peers, name: str, source: str, doc_id: str) -> dict[str, Any]:
    """One document from a peer's archive, stamped with the peer as its origin."""
    record = next((r for r in peers.records() if edges.label(r["name"]) == edges.label(name)), None)
    if record is None:
        raise ValueError(f"no edge from {edges.here()} to {name}")
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
