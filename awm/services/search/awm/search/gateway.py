"""The search service's door to the local gateway: the registry, service calls and event topics."""

from __future__ import annotations

from typing import Any, AsyncIterator

import httpx

from awm import gatewayclient


class Gateway:
    """The calls the archive makes; tests stand in for them."""

    async def services(self) -> list[dict[str, Any]]:
        """The entries of ``awm registry_list``."""
        async with httpx.AsyncClient(timeout=10.0) as client:
            reply = await client.get(f"{gatewayclient.hub_base_url()}/hub/services")
        reply.raise_for_status()
        return (reply.json() or {}).get("services") or []

    async def call(self, service: str, fn: str, args: dict[str, Any], timeout: float) -> Any:
        return await gatewayclient.call(service, fn, args, timeout=timeout)

    def events(self, service: str, topic: str) -> AsyncIterator[Any]:
        return gatewayclient.subscribe(service, topic)
