"""Which peer holds a session this node does not know: the hint for an unknown ``job``.

A session told only a job id cannot tell which node runs it. When the id is not
here, asking each domestic peer's session list turns "no session on this node"
into "it runs on altair: pass peer='altair'".
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

log = logging.getLogger("awm.reflection.elsewhere")

_PEER_TIMEOUT_S = 5.0


def _domestic_peers() -> list[str]:
    from awm import config
    from awm.config import peerbook

    me = config.node_name()
    return [r["name"] for r in peerbook.list_records()
            if r.get("relation") == "domestic" and r["name"] != me]


def _list_sessions(peer: str) -> list[dict[str, Any]]:
    from awm import gatewayclient

    reply = gatewayclient.call_peer_sync(peer, "cx", "list", {},
                                         timeout=_PEER_TIMEOUT_S)
    rows = reply.get("sessions") if isinstance(reply, dict) else None
    return [r for r in rows or [] if isinstance(r, dict)]


def _names(row: dict[str, Any], ref: str) -> bool:
    if ref in (str(row.get("job") or ""), str(row.get("session_id") or "")):
        return True
    return (row.get("name") or "").lower() == ref.lower()


def holders(ref: str, *, peers: list[str] | None = None,
            lister: Callable[[str], list[dict[str, Any]]] = _list_sessions) -> list[str]:
    """The domestic peers whose session list names ``ref``, sorted. A peer that
    does not answer is skipped: this only sharpens an error."""
    try:
        peers = _domestic_peers() if peers is None else peers
    except Exception as exc:  # noqa: BLE001 — an unreadable book means no hint
        log.info("reflection: cannot read the peer book: %s", exc)
        return []

    def probe(peer: str) -> str | None:
        try:
            return peer if any(_names(r, ref) for r in lister(peer)) else None
        except Exception as exc:  # noqa: BLE001
            log.info("reflection: cannot list sessions on %s: %s", peer, exc)
            return None

    if not peers:
        return []
    with ThreadPoolExecutor(max_workers=len(peers)) as pool:
        return sorted(p for p in pool.map(probe, peers) if p)


def hint(ref: str, **kw: Any) -> str:
    """A clause to append to the unknown-job error, or ``""``."""
    found = holders(ref, **kw)
    if not found:
        return ""
    if len(found) == 1:
        return f". It runs on {found[0]}: call again with peer={found[0]!r}"
    return (f". Sessions on {', '.join(found)} answer to it: call again with "
            f"peer=<one of them> and the job id")
