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
        # service_id -> (reason, evictor) staged by ``signal_evicted`` for the
        # WS handler to read (via ``take_eviction``) once ``hold`` returns, so
        # it can close the socket with a who/why notice instead of bare.
        self._eviction: dict[str, tuple[str, str]] = {}
        self._lock = asyncio.Lock()

    async def claim(self, service_id: str, disconnect: asyncio.Event) -> bool:
        """Atomically take the lease slot. ``False`` if someone already holds it.

        Split out of :meth:`hold` so a handler can find out it is a duplicate
        *before* it wires up any per-service state. A rejected duplicate must
        leave no trace of itself on the incumbent — see
        ``api/hub.py::service_control``.
        """
        async with self._lock:
            if service_id in self._holders:
                return False
            self._holders[service_id] = disconnect
            return True

    async def release(self, service_id: str, disconnect: asyncio.Event) -> None:
        """Wait for ``disconnect``, then drop the claim and evict the record.

        The second half of :meth:`hold`. Only ever call it on a slot this
        caller actually won via :meth:`claim`.
        """
        try:
            await disconnect.wait()
        finally:
            async with self._lock:
                self._holders.pop(service_id, None)
            evicted = await self._registry.evict_by_id(service_id)
            if evicted is not None:
                log.info("evicted service %s (id=%s) on lease close",
                         evicted.name, service_id)

    async def hold(self, service_id: str, disconnect: asyncio.Event) -> None:
        """Block until ``disconnect`` fires, then evict the service. Caller
        is the WS handler; it sets ``disconnect`` when the socket closes."""
        if not await self.claim(service_id, disconnect):
            raise LeaseAlreadyHeld(service_id)
        await self.release(service_id, disconnect)

    def is_held(self, service_id: str) -> bool:
        return service_id in self._holders

    def signal_evicted(self, service_id: str, reason: str, evictor: str) -> None:
        """Stage a who/why notice and wake the holder's WS handler.

        Setting the holder's disconnect Event is exactly what a client-side WS
        close does, so ``hold`` unwinds through its normal path. The handler
        then reads the staged notice via ``take_eviction`` and closes the
        socket with code 4410 + ``"evicted by {evictor}: {reason}"``.
        """
        self._eviction[service_id] = (reason, evictor)
        ev = self._holders.get(service_id)
        if ev is not None:
            ev.set()

    def take_eviction(self, service_id: str) -> tuple[str, str] | None:
        """Pop the staged (reason, evictor) notice for ``service_id``, if any."""
        return self._eviction.pop(service_id, None)


class LeaseAlreadyHeld(Exception):
    pass


_singleton: LeaseManager | None = None


def get_lease_manager() -> LeaseManager:
    global _singleton
    if _singleton is None:
        _singleton = LeaseManager()
    return _singleton
