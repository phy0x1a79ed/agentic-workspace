"""Whether cch on another node answers, as seen from this node.

A keeper on a node whose own network drops cannot tell its own outage from
the seat node's. It asks a node elsewhere to look for it.
"""

from __future__ import annotations

from typing import Any

from awm import gatewayclient
from awm.config import node_name

#: Shorter than the keeper's own call to the witness, so a slow peer still
#: gets an answer back to the keeper.
PROBE_TIMEOUT_S = 20.0


async def probe(node: str, *, timeout: float = PROBE_TIMEOUT_S) -> dict[str, Any]:
    """``answers`` says whether `cch list` on ``node`` replied. No session rows travel."""
    try:
        if node == node_name():
            reply = await gatewayclient.call("cch", "list", {}, timeout=timeout)
        else:
            reply = await gatewayclient.call_peer(node, "cch", "list", {}, timeout=timeout)
    except Exception as exc:  # noqa: BLE001 — any failure is "did not answer"
        return {"ok": True, "node": node, "from": node_name(), "answers": False,
                "reason": f"{type(exc).__name__}: {exc}"[:300]}
    if not isinstance(reply, dict) or "sessions" not in reply:
        reason = reply.get("reason") if isinstance(reply, dict) else None
        return {"ok": True, "node": node, "from": node_name(), "answers": False,
                "reason": f"refused: {reason or reply!r}"[:300]}
    daemon = reply.get("daemon") if isinstance(reply.get("daemon"), dict) else {}
    return {"ok": True, "node": node, "from": node_name(), "answers": True,
            "daemon": daemon.get("state")}
