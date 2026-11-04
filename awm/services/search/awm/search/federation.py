"""Asking other nodes: the named peers, or every node this one has an edge to, the same question, each answer apart.

A station runs no search and is never asked. A peer that fails, refuses
or runs an older release shows as a per-peer status and never fails the search.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any

from awm import config
from awm.config import edges
from awm.config.peertoken import node_label
from awm.search.query import FLEET

from awm.search import hits as hitshape

#: Budget for one peer, connect to reply.
PEER_TIMEOUT_S = 10.0
#: The verb every fan-out calls, with `peers=false`, so a search cannot recurse through the federation.
PEER_TOOL = "search_query"
GET_TOOL = "search_get"
#: A reply larger than this is discarded unread.
MAX_REPLY_BYTES = 2 * 1024 * 1024


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
    status: dict[str, Any]
    found: dict[str, list[dict[str, Any]]] = field(default_factory=dict)


def _describe(exc: BaseException) -> str:
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
        return PeerResult(name, {"status": "error", "error": _describe(exc)})
    found = hitshape.sections(reply, _origin(record), limit)
    return PeerResult(name, {"status": "ok", "hits": hitshape.count(found),
                             "sources": _served(reply.get("sources"))}, found)


def _served(sources: Any) -> list[str]:
    """The sources a peer searched. A named source it does not serve comes back absent."""
    if not isinstance(sources, dict):
        return []
    return sorted(n for n, v in sources.items() if not (isinstance(v, dict) and v.get("status") == "absent"))


async def ask(peers: Peers, wanted: list[str] | str, args: dict[str, Any],
              limit: int) -> list[PeerResult]:
    """One result per peer asked. ``wanted`` names the peers; ``"fleet"``, alone or among the names, stands for
    every book peer this node has an edge to."""
    book = {r["name"]: r for r in peers.records()}
    named = [wanted] if isinstance(wanted, str) else wanted
    names: list[str] = []
    for name in named:
        expanded = (sorted(n for n in book if edges.reaches(edges.here(), n))
                    if name == FLEET else [name])
        names += [n for n in expanded if n not in names]
    here = edges.here()
    unknown = [PeerResult(n, {"status": "error", "error": "not in the peer book"})
               for n in names if n not in book]
    unknown += [PeerResult(n, {"status": "error", "error": f"no edge from {here} to {n}"})
                for n in names if n in book and not edges.reaches(here, n)]
    asked = await asyncio.gather(*(_ask_one(peers, book[n], args, limit)
                                   for n in names if n in book and edges.reaches(here, n)))
    return [*asked, *unknown]


#: ``AWM_DOMAIN_HOME_<domain>=<node>`` declares that a domain this node lacks lives on that node alone.
HOME_PREFIX = "AWM_DOMAIN_HOME_"
SOURCES_TOOL = "search_sources"


def homes() -> dict[str, str]:
    """Domain → the node declared its home, the same declaration the gateway routes by."""
    import os
    return {k[len(HOME_PREFIX):]: v.strip() for k, v in os.environ.items()
            if k.startswith(HOME_PREFIX) and k[len(HOME_PREFIX):] and v.strip()}


async def homed_sources(peers: Peers, wanted: dict[str, str]) -> tuple[dict[str, str], set[str]]:
    """Source name → home node for the sources each home's search holds from the domains it is home to,
    and the homes that could not be read, whose last answer the caller keeps."""
    book = {r["name"]: r for r in peers.records()}
    out: dict[str, str] = {}
    unread: set[str] = set()
    for domain, home in wanted.items():
        if home not in book or not edges.reaches(edges.here(), home):
            continue
        try:
            raw = await asyncio.wait_for(peers.invoke(home, SOURCES_TOOL, {}, PEER_TIMEOUT_S),
                                         timeout=PEER_TIMEOUT_S + 1.0)
        except Exception:  # noqa: BLE001 — an unreachable home keeps its last sources
            unread.add(home)
            continue
        if isinstance(raw, dict) and "sources" not in raw and "result" in raw:
            raw = raw["result"]
        listed = raw.get("sources") if isinstance(raw, dict) else None
        for s in listed or []:
            if isinstance(s, dict) and s.get("service") == domain and s.get("name") and not s.get("frozen"):
                out[str(s["name"])] = home
    return out, unread


async def ask_home(peers: Peers, home: str, names: list[str], args: dict[str, Any], limit: int) -> PeerResult:
    """The named sources' sections, asked of the node that is home to them."""
    record = next((r for r in peers.records() if r["name"] == home), None)
    if record is None:
        return PeerResult(home, {"status": "error", "error": "not in the peer book"})
    return await _ask_one(peers, record, {**args, "sources": names}, limit)


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
