"""The search service's door to the local gateway: the registry, service calls and event topics."""

from __future__ import annotations

from typing import Any, AsyncIterator

from awm import gatewayclient


class Gateway:
    """The calls the archive makes; tests stand in for them."""

    async def services(self) -> list[dict[str, Any]]:
        """The rows of ``awm services``."""
        rows = await gatewayclient.call("awm", "services", {}, timeout=10.0)
        return rows if isinstance(rows, list) else []

    async def call(self, tool: str, verb: str, args: dict[str, Any], timeout: float) -> Any:
        return await gatewayclient.call(tool, verb, args, timeout=timeout)

    def events(self, service: str, topic: str) -> AsyncIterator[Any]:
        return gatewayclient.subscribe(service, topic)
