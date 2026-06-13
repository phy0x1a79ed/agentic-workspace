"""WS-lease liveness for hub-registered services.

A service registers (HTTP POST), then opens ``WS /hub/lease/{service_id}``
and holds it for its lifetime. The WS handler calls ``hold`` for the
duration of the connection; on disconnect the registry entry is evicted.
"""

from __future__ import annotations

import asyncio
import logging

from awm.gateway.hub.registry import Registry, get_registry

log = logging.getLogger("awm.hub.lease")


class LeaseManager:
    def __init__(self, registry: Registry | None = None) -> None:
        self._registry = registry or get_registry()
        self._holders: dict[str, asyncio.Event] = {}
        self._lock = asyncio.Lock()

    async def hold(self, service_id: str, disconnect: asyncio.Event) -> None:
        """Block until ``disconnect`` fires, then evict the service. Caller
        is the WS handler; it sets ``disconnect`` when the socket closes."""
        async with self._lock:
            if service_id in self._holders:
                raise LeaseAlreadyHeld(service_id)
            self._holders[service_id] = disconnect
        try:
            await disconnect.wait()
        finally:
            async with self._lock:
                self._holders.pop(service_id, None)
            evicted = await self._registry.evict_by_id(service_id)
            if evicted is not None:
                log.info("evicted service %s (id=%s) on lease close",
                         evicted.name, service_id)

    def is_held(self, service_id: str) -> bool:
        return service_id in self._holders


class LeaseAlreadyHeld(Exception):
    pass


_singleton: LeaseManager | None = None


def get_lease_manager() -> LeaseManager:
    global _singleton
    if _singleton is None:
        _singleton = LeaseManager()
    return _singleton
