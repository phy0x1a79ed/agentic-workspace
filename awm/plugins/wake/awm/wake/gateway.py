"""Calls to other plugins, only through the gateway, by tool name (install.md R117 items 1 and 2).

A call that names a ``peer`` goes to the local ``/invoke`` too: the gateway
carries it over the ssh edge.
"""

from __future__ import annotations

from typing import Any

import httpx

from awm import gatewayclient


REFLECTION = "reflection"
WAKE = "wake"
#: The contract a harness plugin implements; its provider serves ``revive_pinned``.
HARNESS_CONTRACT = "harness@1"


class CallError(RuntimeError):
    pass


class Gateway:
    def __init__(self, hub_url: str | None = None, timeout: float = 60.0):
        self.hub_url = (hub_url or gatewayclient.hub_base_url()).rstrip("/")
        self.timeout = timeout

    async def call(self, name: str, verb: str, args: dict | None = None,
                   peer: str | None = None) -> Any:
        try:
            return await gatewayclient.call(name, verb, args, peer=peer, timeout=self.timeout)
        except httpx.HTTPError as exc:
            raise CallError(f"{name}.{verb}: gateway unreachable: {exc}") from exc
        except gatewayclient.GatewayCallError as exc:
            raise CallError(f"{name}.{verb}: HTTP {exc.status}: {exc.body[:300]}") from exc

    async def harness(self) -> str | None:
        """The tool of this node's harness provider, or None where the gateway names none.

        The Rust gateway answers ``/hub/contract/harness@1``.
        """
        try:
            async with httpx.AsyncClient(timeout=5.0) as cli:
                resp = await cli.get(f"{self.hub_url}/hub/contract/{HARNESS_CONTRACT}")
            providers = resp.json() if resp.status_code // 100 == 2 else []
        except (httpx.HTTPError, ValueError):
            return None
        local = [p for p in providers if isinstance(p, dict) and not p.get("peer") and p.get("name")]
        return str(local[0]["name"]) if local else None

    async def up(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=5.0) as cli:
                resp = await cli.get(f"{self.hub_url}/status")
            return resp.status_code // 100 == 2
        except httpx.HTTPError:
            return False
