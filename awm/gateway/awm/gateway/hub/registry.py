"""In-memory registry of forwarded services.

Each URL path prefix maps to a *stack* of records (one base + zero or more
overlays). The forwarding middleware asks ``longest_match`` for the active
record at each request; the active record is the topmost overlay if any are
attached, otherwise the base.

Record kinds:

* ``kind="url"`` — proxy HTTP/WS requests to ``url`` (external svc; preserved
  for non-package registrations).
* ``kind="static"`` — serve files from ``static_dir`` under the prefix
  (preserved for non-package registrations).
* ``kind="page"`` — like ``static`` but prefix must begin ``/ui/``; never
  proxies a backend. The hub's canonical page-bundle shape.
* ``kind="service"`` — RPC-over-WS service. The hub owns a persistent
  control WS and on-demand bridge WSs per direct session/emitter; clients
  see the service at ``/svc/<name>/{fn,emit,session}/*``. PID + start
  metadata travel on the record so the supervisor can restart the service
  if its control WS goes silent.

Overlay shadowing (G5):

* A *base* registration is the normal one — POST /hub/service/register or
  POST /hub/page/register (one base per prefix, conflict on duplicate).
* A *shadow* overlay is pushed via POST /hub/shadow/register; it requires
  a base already exist, and on lease close it is popped (base never
  popped by shadow lifecycle). Multiple overlays stack LIFO.

No persistence in the registry itself — supervisor.py journals service
records separately so the hub can resume them across hub restarts.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Literal

log = logging.getLogger("awm.hub.registry")


Kind = Literal["url", "static", "page", "service"]


@dataclass
class ServiceRecord:
    name: str
    prefix: str
    kind: Kind = "url"
    url: str = ""                                   # kind == "url"
    static_dir: str = ""                            # kind in {"static","page"}
    entry: str | None = None                        # auto-shell entry (static only)
    css: tuple[str, ...] = ()                       # auto-shell stylesheets (static only)
    mount_id: str = "app"                           # auto-shell mount node id (static only)

    # Service-only metadata. ``api`` carries the manifest the service sent
    # in its `ready` frame (functions/emitters/sessions). The control WS
    # state lives on the rpc.py ControlChannel keyed by service_id.
    api: dict[str, Any] = field(default_factory=dict)
    start_cmd: list[str] = field(default_factory=list)
    cwd: str = ""

    # Lifecycle bookkeeping for kind="service". ``backend_status`` mirrors
    # the ready state the client surface gates on.
    backend_status: str = "starting"                # starting | ready | down
    backend_pid: int | None = None

    teardown: Callable[[], Awaitable[None]] | None = field(
        default=None, repr=False, compare=False
    )
    service_id: str = field(default_factory=lambda: secrets.token_urlsafe(16))
    # Whether this record is an overlay (pushed via shadow/register) or
    # the base for its prefix. Eviction pops overlays but never collapses
    # the base on shadow lifecycle.
    is_overlay: bool = False


class PrefixConflict(Exception):
    """A different live base already owns this prefix, or the prefix is reserved."""


class NoBaseToShadow(Exception):
    """Tried to push a shadow overlay on a prefix with no base record."""


# Reserved control-plane prefixes. /svc/ and /ui/ are claimable by
# services/pages respectively — they are NOT reserved here; the per-kind
# registration validators enforce the right shape.
_RESERVED_PREFIXES: tuple[str, ...] = ("/hub",)


class Registry:
    def __init__(self) -> None:
        # prefix -> [base_record, *overlay_records_oldest_to_newest]
        self._stacks: dict[str, list[ServiceRecord]] = {}
        # (kind, name) -> record. Keyed by (kind, name) so a package can
        # ship a same-named page (/ui/X) and service (/svc/X) — they share
        # logical identity but live on disjoint prefixes.
        self._by_name: dict[tuple[str, str], ServiceRecord] = {}
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Generic base registrations
    # ------------------------------------------------------------------

    async def register(self, name: str, prefix: str, url: str) -> ServiceRecord:
        prefix = _normalize_prefix(prefix)
        async with self._lock:
            self._check_register(prefix, name, "url", allow_shadow=False)
            rec = ServiceRecord(
                name=name, prefix=prefix, kind="url", url=url.rstrip("/"),
            )
            self._install_base(rec)
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
            self._check_register(prefix, name, "static", allow_shadow=False)
            rec = ServiceRecord(
                name=name,
                prefix=prefix,
                kind="static",
                static_dir=static_dir,
                entry=entry,
                css=tuple(css),
                mount_id=mount_id,
            )
            self._install_base(rec)
            return rec

    async def register_page(
        self,
        name: str,
        prefix: str,
        static_dir: str,
    ) -> ServiceRecord:
        """Register a page bundle. Prefix must begin /ui/."""
        prefix = _normalize_prefix(prefix)
        if not (prefix == "/ui" or prefix.startswith("/ui/")):
            raise PrefixConflict(
                f"page prefix {prefix!r} must begin with /ui/"
            )
        async with self._lock:
            self._check_register(prefix, name, "page", allow_shadow=False)
            rec = ServiceRecord(
                name=name,
                prefix=prefix,
                kind="page",
                static_dir=static_dir,
            )
            rec.backend_status = "ready"  # static; nothing to wait for
            self._install_base(rec)
            return rec

    async def register_service(
        self,
        name: str,
        prefix: str,
        *,
        pid: int | None,
        start_cmd: list[str],
        cwd: str,
        teardown: Callable[[], Awaitable[None]] | None = None,
    ) -> ServiceRecord:
        """Register a service registration. Prefix must begin /svc/.

        The service is born ``backend_status="starting"``; the control-WS
        handler flips it to ``ready`` once the service sends its ``ready``
        frame and to ``down`` on close.
        """
        prefix = _normalize_prefix(prefix)
        if not (prefix == "/svc" or prefix.startswith("/svc/")):
            raise PrefixConflict(
                f"service prefix {prefix!r} must begin with /svc/"
            )
        async with self._lock:
            self._check_register(prefix, name, "service", allow_shadow=False)
            rec = ServiceRecord(
                name=name,
                prefix=prefix,
                kind="service",
                start_cmd=list(start_cmd),
                cwd=cwd,
                backend_pid=pid,
                backend_status="starting",
                teardown=teardown,
            )
            self._install_base(rec)
            return rec

    # ------------------------------------------------------------------
    # Shadow overlays
    # ------------------------------------------------------------------

    async def push_shadow(self, rec: ServiceRecord) -> ServiceRecord:
        """Push ``rec`` as an overlay on its prefix.

        Requires that a base record already exist for that prefix. The
        overlay is pushed LIFO and becomes the active record returned by
        ``longest_match``. On eviction (lease close) the overlay is popped
        and traffic falls back to whatever is now topmost (the next-most-
        recent overlay if any, or the base).
        """
        prefix = _normalize_prefix(rec.prefix)
        rec.prefix = prefix
        rec.is_overlay = True
        async with self._lock:
            stack = self._stacks.get(prefix)
            if not stack:
                raise NoBaseToShadow(
                    f"no base registered for prefix {prefix!r}; cannot shadow"
                )
            key = (rec.kind, rec.name)
            if key in self._by_name:
                raise PrefixConflict(
                    f"name {rec.name!r} already in use for kind {rec.kind!r}; pick a unique shadow name"
                )
            stack.append(rec)
            self._by_name[key] = rec
            log.info(
                "pushed shadow %s on %s (depth=%d)",
                rec.name, prefix, len(stack) - 1,
            )
            return rec

    # ------------------------------------------------------------------
    # Eviction
    # ------------------------------------------------------------------

    async def evict_by_id(self, service_id: str) -> ServiceRecord | None:
        async with self._lock:
            evicted = self._pop_by_id_locked(service_id)
        if evicted is not None and evicted.teardown is not None:
            try:
                await evicted.teardown()
            except Exception:
                log.exception("teardown failed for %s", evicted.name)
        return evicted

    async def evict_by_name(self, name: str, *, kind: str | None = None) -> ServiceRecord | None:
        """Evict by name. With ``kind`` given, target that specific record.
        Without ``kind``, evict iff exactly one record holds the name;
        raise ``PrefixConflict`` (with the candidate kinds) if more do.
        """
        async with self._lock:
            if kind is not None:
                rec = self._by_name.get((kind, name))
            else:
                matches = [r for (k, n), r in self._by_name.items() if n == name]
                if len(matches) > 1:
                    kinds = sorted({r.kind for r in matches})
                    raise PrefixConflict(
                        f"name {name!r} is ambiguous across kinds {kinds!r}; specify kind"
                    )
                rec = matches[0] if matches else None
            if rec is None:
                return None
            evicted = self._pop_by_id_locked(rec.service_id)
        if evicted is not None and evicted.teardown is not None:
            try:
                await evicted.teardown()
            except Exception:
                log.exception("teardown failed for %s", evicted.name)
        return evicted

    def _pop_by_id_locked(self, service_id: str) -> ServiceRecord | None:
        for prefix, stack in list(self._stacks.items()):
            for idx, rec in enumerate(stack):
                if rec.service_id == service_id:
                    stack.pop(idx)
                    self._by_name.pop((rec.kind, rec.name), None)
                    if not stack:
                        # Base was evicted directly via DELETE.
                        del self._stacks[prefix]
                    log.info(
                        "evicted %s (id=%s, was_overlay=%s, remaining=%d)",
                        rec.name, service_id, rec.is_overlay, len(stack),
                    )
                    return rec
        return None

    # ------------------------------------------------------------------
    # Lookup
    # ------------------------------------------------------------------

    async def list(self) -> list[ServiceRecord]:
        async with self._lock:
            out: list[ServiceRecord] = []
            for stack in self._stacks.values():
                out.extend(stack)
            return out

    def longest_match(self, path: str) -> ServiceRecord | None:
        """Resolve a request path to the active record (topmost overlay
        or base) for the longest matching prefix.

        Sync; tolerates concurrent mutation (snapshot iteration is GIL-safe).
        """
        if not self._stacks:
            return None
        best_prefix: str | None = None
        best_stack: list[ServiceRecord] | None = None
        for prefix, stack in self._stacks.items():
            if path == prefix or path.startswith(prefix + "/"):
                if best_prefix is None or len(prefix) > len(best_prefix):
                    best_prefix = prefix
                    best_stack = stack
        if best_stack is None:
            return None
        return best_stack[-1]  # topmost

    def is_empty(self) -> bool:
        return not self._stacks

    def get_by_id(self, service_id: str) -> ServiceRecord | None:
        for stack in self._stacks.values():
            for rec in stack:
                if rec.service_id == service_id:
                    return rec
        return None

    def get_by_name(self, kind: str, name: str) -> ServiceRecord | None:
        return self._by_name.get((kind, name))

    def service_records(self) -> list[ServiceRecord]:
        """Snapshot of the topmost ``kind=="service"`` record per prefix.

        Sync; tolerates concurrent mutation (snapshot iteration is
        GIL-safe), so the catalog can render ``/tools`` without taking the
        async lock. Returns the active (topmost overlay or base) record for
        each prefix, filtered to services — the ones that carry an ``api``
        manifest.
        """
        out: list[ServiceRecord] = []
        for stack in list(self._stacks.values()):
            if stack and stack[-1].kind == "service":
                out.append(stack[-1])
        return out

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _check_register(self, prefix: str, name: str, kind: str, *, allow_shadow: bool) -> None:
        for reserved in _RESERVED_PREFIXES:
            if prefix == reserved or prefix.startswith(reserved + "/"):
                raise PrefixConflict(
                    f"prefix {prefix!r} is reserved by the hub control plane"
                )
        existing_stack = self._stacks.get(prefix)
        if existing_stack and not allow_shadow:
            # Idempotent re-register: same name + same prefix replaces the
            # base in place (caller's existing record vanishes; new one
            # takes over). Conflict only if a DIFFERENT name owns the
            # prefix.
            base = existing_stack[0]
            if base.name != name:
                raise PrefixConflict(
                    f"prefix {prefix!r} already registered by {base.name!r}"
                )
            return
        clash = self._by_name.get((kind, name))
        if clash is not None and clash.prefix != prefix:
            raise PrefixConflict(
                f"name {name!r} already in use for kind {kind!r} under {clash.prefix!r}"
            )

    def _install_base(self, rec: ServiceRecord) -> None:
        # Drop any prior base for this name/prefix combination so a
        # re-register replaces in place rather than appending.
        existing = self._stacks.get(rec.prefix)
        if existing:
            old = existing[0]
            self._by_name.pop((old.kind, old.name), None)
        self._stacks[rec.prefix] = [rec]
        self._by_name[(rec.kind, rec.name)] = rec


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


def _reset_for_tests() -> None:
    """Test-only: drop the singleton so the next get_registry() starts empty."""
    global _singleton
    _singleton = None
