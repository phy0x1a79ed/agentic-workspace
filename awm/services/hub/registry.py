"""In-memory registry of forwarded services.

A ``ServiceRecord`` claims one URL path prefix. The forwarding middleware
asks ``longest_match`` on each request; matched → forward, miss → fall
through to in-process routers.

No persistence: lease holders re-register on hub restart. State is
``asyncio.Lock``-guarded so register/evict don't race with lookups.
"""

from __future__ import annotations

import asyncio
import secrets
from dataclasses import dataclass, field


@dataclass
class ServiceRecord:
    name: str
    prefix: str
    url: str
    service_id: str = field(default_factory=lambda: secrets.token_urlsafe(16))


class PrefixConflict(Exception):
    """A different live record already owns this prefix."""


class Registry:
    def __init__(self) -> None:
        self._by_name: dict[str, ServiceRecord] = {}
        self._lock = asyncio.Lock()

    async def register(self, name: str, prefix: str, url: str) -> ServiceRecord:
        prefix = _normalize_prefix(prefix)
        async with self._lock:
            for rec in self._by_name.values():
                if rec.prefix == prefix and rec.name != name:
                    raise PrefixConflict(
                        f"prefix {prefix!r} already registered by {rec.name!r}"
                    )
            rec = ServiceRecord(name=name, prefix=prefix, url=url.rstrip("/"))
            self._by_name[name] = rec
            return rec

    async def evict_by_id(self, service_id: str) -> ServiceRecord | None:
        async with self._lock:
            for name, rec in list(self._by_name.items()):
                if rec.service_id == service_id:
                    del self._by_name[name]
                    return rec
            return None

    async def evict_by_name(self, name: str) -> ServiceRecord | None:
        async with self._lock:
            return self._by_name.pop(name, None)

    async def list(self) -> list[ServiceRecord]:
        async with self._lock:
            return list(self._by_name.values())

    def longest_match(self, path: str) -> ServiceRecord | None:
        """Sync lookup — hot path. Tolerates concurrent mutation: dict
        iteration over a snapshot is GIL-safe; worst case is one stale
        read between the writer's evict and the next request."""
        if not self._by_name:
            return None
        best: ServiceRecord | None = None
        for rec in self._by_name.values():
            if path == rec.prefix or path.startswith(rec.prefix + "/"):
                if best is None or len(rec.prefix) > len(best.prefix):
                    best = rec
        return best

    def is_empty(self) -> bool:
        return not self._by_name


def _normalize_prefix(prefix: str) -> str:
    if not prefix.startswith("/"):
        prefix = "/" + prefix
    if len(prefix) > 1 and prefix.endswith("/"):
        prefix = prefix.rstrip("/")
    return prefix


_singleton: Registry | None = None


def get_registry() -> Registry:
    global _singleton
    if _singleton is None:
        _singleton = Registry()
    return _singleton
