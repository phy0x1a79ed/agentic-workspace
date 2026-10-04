"""Compose the render pipeline and hold the view mount's gateway lease.

drawio splits this concern into two files: its own ``mount.py`` registers the
static editor bytes at ``/drawio-app`` and holds that lease, while a separate
``ViewServer`` inside ``view.py`` registers the render-URL listener at
``/drawio-app/view`` and holds *that* lease. penpot-view ships no static
web-client tree at all -- the whole service *is* the view mount -- so there is
exactly one registration to hold, and :mod:`awm.penpot_view.view` already owns
it end to end: :class:`~awm.penpot_view.view.ViewServer` implements the same
register / hold-lease / reconnect-loop pattern as drawio's two mount modules,
verbatim (see its own docstring and :meth:`~awm.penpot_view.view.ViewServer.
hold_mount`). Reimplementing that loop here would be a second copy of code
that already has to stay correct in one place -- this module's job is
composition, not a rewrite: build the real collaborators (an
:class:`~awm.penpot_view.exporter_client.ExporterClient`, a cache wired to its
freshness probe, a renderer over both) and hand the result to
:class:`~awm.penpot_view.view.ViewServer` to hold.

**Wiring ``freshness``.** :class:`~awm.penpot_view.view.Cache` accepts an
optional ``freshness(file_id, known_etag) -> (changed, etag)`` hook; left
unset it degrades to plain TTL expiry, which the module docstring on
``view.py`` calls out as materially weaker -- every entry re-renders on a
timer whether or not anything actually changed. :meth:`ExporterClient.
file_etag` *is* that hook (matching signature exactly), so
:func:`build_view_server` always passes it. There is no code path here that
constructs a ``Cache`` without it; a build that skipped it would still run,
just silently worse, which is exactly the failure mode worth refusing to leave
implicit.

**One instance per process.** :func:`build_view_server` is a pure factory --
each call returns a fresh, unstarted :class:`~awm.penpot_view.view.ViewServer`
with no listener bound and no lease held, which is what makes it safe to call
repeatedly from a test. Production code must never call it more than once:
:func:`view_server` is the single lazily-built instance the running service
holds, and :func:`hold_mount`/:func:`status` both go through it.
"""

from __future__ import annotations

import logging

from . import renderspec as R
from . import view as V
from .exporter_client import ExporterClient

log = logging.getLogger("awm.penpot_view.mount")

#: Re-exported so a caller never has to import both this module and
#: ``renderspec``/``view`` just to recognise or reason about the mount.
VIEW_PREFIX = R.VIEW_PREFIX
MOUNT_NAME = V.MOUNT_NAME

_VIEW: V.ViewServer | None = None


def build_view_server() -> V.ViewServer:
    """Build one render pipeline: exporter -> freshness-wired cache -> renderer
    -> view mount. Side-effect-free beyond object construction -- no listener
    is bound and no gateway lease is registered until ``hold_mount()`` runs.
    """
    exporter = ExporterClient()
    cache = V.Cache(ttl=V.DEFAULT_TTL, cold_timeout=V.COLD_RENDER_TIMEOUT,
                    freshness=exporter.file_etag)
    renderer = V.Renderer(exporter, cache=cache)
    return V.ViewServer(renderer)


def view_server() -> V.ViewServer:
    """The process-wide :class:`~awm.penpot_view.view.ViewServer`, built on
    first use and reused after -- the single mount this process holds."""
    global _VIEW
    if _VIEW is None:
        _VIEW = build_view_server()
    return _VIEW


def status() -> dict:
    """Live mount status -- the same shape a status verb reports, so a caller
    that only wants "is it mounted" never has to build a ``ViewServer`` first."""
    return view_server().status()


async def hold_mount() -> None:
    """Register the view listener's ``kind=url`` gateway mount and hold its
    lease for as long as this process runs.

    Delegates to :meth:`ViewServer.hold_mount`, which never returns and
    retries every fault forever -- see that method and the module docstring
    above for why this does not reimplement the loop itself. The caller
    (the service entry point) must run this as a background task, never await
    it inline, or the control WS it also needs to serve would never come up.
    """
    await view_server().hold_mount()
