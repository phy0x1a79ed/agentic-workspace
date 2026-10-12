"""The peer fan-out: ask each node in the book the same question, report each answer apart.

Every peer answers through its own gateway gate, which keeps only the sources
that peer granted this node, so what a peer returns is already narrowed to its
grants. A peer that fails, refuses or runs an older release shows as a per-peer
status and never fails the search or hides the local hits.
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


def _unsupported(exc: BaseException) -> bool:
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
    try:
        raw = await asyncio.wait_for(peers.invoke(name, PEER_TOOL, args, PEER_TIMEOUT_S),
                                     timeout=PEER_TIMEOUT_S + 1.0)
        reply = _reply(raw)
    except (asyncio.TimeoutError, TimeoutError):
        return PeerResult(name, [], {"status": "error", "error": f"timeout after {PEER_TIMEOUT_S:g}s"})
    except Exception as exc:  # noqa: BLE001 — a peer's failure is reported, never raised
        if _unsupported(exc):
            return PeerResult(name, [], {"status": "unsupported",
                                         "detail": "no search tool there, or no source granted to this node"})
        return PeerResult(name, [], {"status": "error", "error": _describe(exc)})
    origin = _origin(record)
    hits = [h for h in (_hit(x, origin) for x in reply["hits"]) if h][:limit]
    sources = reply.get("sources")
    return PeerResult(name, hits, {"status": "ok", "hits": len(hits),
                                   "sources": sorted(sources) if isinstance(sources, dict) else []})


async def ask(peers: Peers, wanted: list[str] | None, args: dict[str, Any],
              limit: int) -> list[PeerResult]:
    """One result per peer asked. ``wanted`` names the peers to ask; ``None`` asks every peer in the book."""
    book = {r["name"]: r for r in peers.records()}
    names = sorted(book) if wanted is None else wanted
    unknown = [PeerResult(n, [], {"status": "error", "error": "not in the peer book"})
               for n in names if n not in book]
    asked = await asyncio.gather(*(_ask_one(peers, book[n], args, limit) for n in names if n in book))
    return [*asked, *unknown]
