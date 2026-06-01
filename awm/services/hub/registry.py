"""In-memory registry of forwarded services.

A ``ServiceRecord`` claims one URL path prefix. The forwarding middleware
asks ``longest_match`` on each request; matched → forward, miss → fall
through to in-process routers.

Three registration kinds share one record shape:

* ``kind="url"`` — proxy HTTP/WS requests to ``url`` (the original svc-* model).
* ``kind="static"`` — serve files from ``static_dir`` under the prefix;
  if the dir has no ``index.html`` and ``entry`` is set, the hub renders a
  minimal ESM shell that mounts the bundle at ``mount_id``.
* ``kind="stripe"`` — static frontend at the prefix root + supervised
  backend reached at ``<prefix>/_api/*``. The hub spawns the backend at
  register time, allocates a port, injects ``AWM_SERVICE_PORT``, polls
  the declared health endpoint, and SIGTERMs on lease close. See
  ``supervisor.py`` for the lifecycle.

No persistence: lease holders re-register on hub restart. State is
``asyncio.Lock``-guarded so register/evict don't race with lookups.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Literal

log = logging.getLogger("awm.hub.registry")


@dataclass
class ServiceRecord:
    name: str
    prefix: str
    kind: Literal["url", "static", "stripe"] = "url"
    url: str = ""                                   # kind == "url"
    static_dir: str = ""                            # kind in {"static", "stripe"} — absolute
    entry: str | None = None                        # auto-shell entry script (static)
    css: tuple[str, ...] = ()                       # auto-shell stylesheets (static)
    mount_id: str = "app"                           # auto-shell mount node id (static)
    # Stripe-only: supervised backend bookkeeping. The supervisor owns
    # the process; the registry just carries enough metadata for the
    # routing middleware to dispatch _api/ requests and for /hub/stripes
    # to surface status.
    backend_port: int | None = None
    backend_status: str = "starting"                # "starting" | "ready" | "down"
    backend_pid: int | None = None
    teardown: Callable[[], Awaitable[None]] | None = field(
        default=None, repr=False, compare=False
    )
    service_id: str = field(default_factory=lambda: secrets.token_urlsafe(16))


class PrefixConflict(Exception):
    """A different live record already owns this prefix, or the prefix is reserved."""


# Reserved control-plane prefixes. The forwarding middleware already
# refuses to forward /hub/* defensively; we also reject at register time
# so the registry can't grow records that would never be reachable.
_RESERVED_PREFIXES: tuple[str, ...] = ("/hub",)


class Registry:
    def __init__(self) -> None:
        self._by_name: dict[str, ServiceRecord] = {}
        self._lock = asyncio.Lock()

    async def register(self, name: str, prefix: str, url: str) -> ServiceRecord:
        prefix = _normalize_prefix(prefix)
        async with self._lock:
            self._check_prefix(prefix, name)
            rec = ServiceRecord(
                name=name, prefix=prefix, kind="url", url=url.rstrip("/"),
            )
            self._by_name[name] = rec
            return rec

    async def register_static(
        self,
        name: str,
        prefix: str,
        static_dir: str,
        *,
        entry: str | None = None,
        css: tuple[str, ...] = (),
        mount_id: str = "app",
    ) -> ServiceRecord:
        prefix = _normalize_prefix(prefix)
        async with self._lock:
            self._check_prefix(prefix, name)
            rec = ServiceRecord(
                name=name,
                prefix=prefix,
                kind="static",
                static_dir=static_dir,
                entry=entry,
                css=tuple(css),
                mount_id=mount_id,
            )
            self._by_name[name] = rec
            return rec

    async def register_stripe(
        self,
        name: str,
        prefix: str,
        static_dir: str,
        *,
        has_backend: bool,
        backend_port: int | None = None,
        teardown: Callable[[], Awaitable[None]] | None = None,
    ) -> ServiceRecord:
        """Create a stripe record. ``has_backend`` decides initial
        ``backend_status``: a backendless stripe is born ``ready``;
        a stripe whose backend is about to be spawned is born
        ``starting`` and flips to ``ready`` only after the health
        poll confirms the bind. Don't infer this from ``backend_port``
        — it's None during the brief window between record creation
        and ``spawn_backend`` returning even when a backend exists,
        and routing must NOT proxy to a non-existent backend during
        that window."""
        prefix = _normalize_prefix(prefix)
        async with self._lock:
            self._check_prefix(prefix, name)
            rec = ServiceRecord(
                name=name,
                prefix=prefix,
                kind="stripe",
                static_dir=static_dir,
                backend_port=backend_port,
                backend_status="starting" if has_backend else "ready",
                teardown=teardown,
            )
            self._by_name[name] = rec
            return rec

    async def evict_by_id(self, service_id: str) -> ServiceRecord | None:
        async with self._lock:
            evicted: ServiceRecord | None = None
            for name, rec in list(self._by_name.items()):
                if rec.service_id == service_id:
                    del self._by_name[name]
                    evicted = rec
                    break
        if evicted is not None and evicted.teardown is not None:
            try:
                await evicted.teardown()
            except Exception:
                log.exception("teardown failed for %s", evicted.name)
        return evicted

    async def evict_by_name(self, name: str) -> ServiceRecord | None:
        async with self._lock:
            evicted = self._by_name.pop(name, None)
        if evicted is not None and evicted.teardown is not None:
            try:
                await evicted.teardown()
            except Exception:
                log.exception("teardown failed for %s", evicted.name)
        return evicted

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

    def _check_prefix(self, prefix: str, name: str) -> None:
        for reserved in _RESERVED_PREFIXES:
            if prefix == reserved or prefix.startswith(reserved + "/"):
                raise PrefixConflict(
                    f"prefix {prefix!r} is reserved by the hub control plane"
                )
        for rec in self._by_name.values():
            if rec.prefix == prefix and rec.name != name:
                raise PrefixConflict(
                    f"prefix {prefix!r} already registered by {rec.name!r}"
                )


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
