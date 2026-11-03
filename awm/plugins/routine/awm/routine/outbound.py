"""Everything the routine service asks of other plugins, behind one interface.

The service calls other plugins only through the gateway (install.md R117), on
the `POST /invoke` door that today's Python gateway and the Rust gateway both
serve. Tests replace :class:`Outbound` with a fake.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Protocol

import httpx

log = logging.getLogger("awm.routine.outbound")

PLUGIN = "routine"


class Outbound(Protocol):
    async def start_session(self, *, peer: str, project: str, scope: str,
                            prompt: str, started_by: str, name: str) -> dict[str, Any]:
        """Start a background session. Returns at least ``{"job": ...}``."""

    async def notify_operator(self, text: str) -> None:
        """Send the operator a Discord DM."""

    async def journal(self, project: str, scope: str, body: str) -> None:
        """Post to a scope's journal."""

    async def invoke(self, tool: str, verb: str, args: dict[str, Any],
                     *, peer: str | None = None, timeout: float = 60.0) -> Any:
        """Call any tool verb, here or on a peer."""


class GatewayOutbound:
    """The real outbound: `POST $AWM_HUB_URL/invoke`."""

    def __init__(self, hub_url: str | None = None) -> None:
        self.hub_url = (hub_url or os.environ.get("AWM_HUB_URL")
                        or "http://127.0.0.1:8765").rstrip("/")

    async def invoke(self, tool: str, verb: str, args: dict[str, Any],
                     *, peer: str | None = None, timeout: float = 60.0) -> Any:
        inner: dict[str, Any] = {"verb": verb, "args": args}
        if peer and peer != "local":
            inner["peer"] = peer
        async with httpx.AsyncClient(timeout=timeout) as cli:
            resp = await cli.post(f"{self.hub_url}/invoke",
                                  json={"name": tool, "args": inner},
                                  headers={"X-Awm-Plugin": PLUGIN})
        if resp.status_code >= 300:
            raise RuntimeError(f"{tool} {verb}: HTTP {resp.status_code}: {resp.text[:400]}")
        body = resp.json()
        result = body.get("result", body) if isinstance(body, dict) else body
        if isinstance(result, str):
            try:
                result = json.loads(result)
            except ValueError:
                pass
        if isinstance(result, dict) and result.get("error"):
            raise RuntimeError(f"{tool} {verb}: {result['error']}")
        return result

    async def start_session(self, *, peer: str, project: str, scope: str,
                            prompt: str, started_by: str, name: str) -> dict[str, Any]:
        res = await self.invoke("reflection", "start_other", {
            "project": project, "scope": scope, "prompt": prompt,
            "started_by": started_by, "name": name,
        }, peer=peer, timeout=120.0)
        if not isinstance(res, dict) or not res.get("job"):
            raise RuntimeError(f"start_other returned no job: {res!r}"[:400])
        return res

    async def notify_operator(self, text: str) -> None:
        from awm.config.peerbook import operator_dm
        target = operator_dm()
        if target is None:
            raise RuntimeError("this node has no operator DM channel")
        account, channel = target
        await self.invoke("social", "send", {
            "account": account, "channel": channel, "text": text},
            peer=os.environ.get("AWM_SOCIAL_PEER") or None)

    async def journal(self, project: str, scope: str, body: str) -> None:
        await self.invoke("journal", "post", {
            "project": project, "scope": scope, "author": "routine",
            "kind": "message", "body": body})
