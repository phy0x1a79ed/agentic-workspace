"""Calls to other plugins through this node's gateway, as the relay's Refused and Unreachable."""

from __future__ import annotations

import json
from typing import Any

import httpx

from awm import gatewayclient
from awm.operations.relay import Refused, Unreachable


def _fault(exc: Exception) -> Exception:
    """A transport failure, a 5xx or a retryable 4xx is Unreachable; any other 4xx is Refused."""
    if not isinstance(exc, gatewayclient.GatewayCallError):
        return Unreachable(str(exc))
    if exc.status >= 500 or exc.status in (404, 408, 429):
        return Unreachable(f"{exc.status} {exc.body[:200]}")
    try:
        detail = json.loads(exc.body).get("detail", exc.body)
    except (ValueError, AttributeError):
        detail = exc.body or str(exc.status)
    return Refused(detail)


class GatewayTool:
    """One tool reached by name."""

    def __init__(self, name: str, *, peer: str | None = None, timeout: float = 15.0) -> None:
        self.name, self.peer, self.timeout = name, peer, timeout

    async def call(self, verb: str, args: dict[str, Any], *, timeout: float | None = None,
                   peer: str | None = None) -> Any:
        try:
            return await gatewayclient.call(self.name, verb, args, peer=peer or self.peer,
                                            timeout=timeout or self.timeout)
        except (gatewayclient.GatewayCallError, httpx.HTTPError) as exc:
            raise _fault(exc) from exc

    def call_sync(self, verb: str, args: dict[str, Any], *, timeout: float | None = None) -> Any:
        try:
            return gatewayclient.call_sync(self.name, verb, args, peer=self.peer,
                                           timeout=timeout or self.timeout)
        except (gatewayclient.GatewayCallError, httpx.HTTPError) as exc:
            raise _fault(exc) from exc


async def usage(page: str) -> None:
    """Record one page visit (plugin contract, § Pages on their own origin)."""
    try:
        async with httpx.AsyncClient(timeout=3) as c:
            await c.post(f"{gatewayclient.hub_base_url()}/usage",
                         json={"surface": "page", "plugin": "operations", "name": page})
    except httpx.HTTPError:
        pass


class ApproverTools:
    """The tools the approver calls: 2fa on its own node (R137), and zotero behind deneb's
    singleton door, which takes Sync now orders (fleet.md R83, R79)."""

    def __init__(self) -> None:
        self.twofa_tool = GatewayTool("2fa", timeout=20)
        self.zotero_tool = GatewayTool("zotero", timeout=60)

    async def twofa(self, verb: str, args: dict[str, Any]) -> Any:
        return await self.twofa_tool.call(verb, args)

    async def zotero(self, verb: str, args: dict[str, Any]) -> Any:
        return await self.zotero_tool.call(verb, args)
