"""The verb surface, and the process entry point that serves it.

drawio keeps these as two files -- ``service.py`` is the diagram business
logic (checkouts, merges, live tabs), and a separate ``hub_adapter.py`` builds
the manifest, registers the process with the gateway's control WS, and starts
the background mount tasks. penpot-view has no diagram-shaped business logic
of its own: everything it does *is* the view mount that :mod:`awm.penpot_view.
mount` already composes. So there is nothing left to put in a second file, and
this module plays both roles drawio splits apart -- the three verbs an
operator or agent calls (:func:`status`, :func:`force_refresh`,
:func:`cache_stats`), and, below the ``-- entry point --`` line, the
:class:`~awm.gatewayclient.ServiceAdapter` wiring ``run.sh`` execs as
``python -m awm.penpot_view.service``.

Every verb here operates on the single :class:`~awm.penpot_view.view.
ViewServer` instance :mod:`awm.penpot_view.mount` builds and holds -- there is
exactly one render pipeline per process, matching the one gateway lease that
pipeline's listener holds.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from awm.gatewayclient import ServiceAdapter

from . import demo as D
from . import exporter_client as EC
from . import mount
from . import renderspec as R

log = logging.getLogger("awm.penpot_view.service")


# --- verbs -------------------------------------------------------------

def status() -> dict:
    """Whether the view mount is up, and whether a service account is even
    configured -- the two things worth checking before blaming a blank render
    on something else."""
    return {
        "penpot_base_url": EC.DEFAULT_BASE_URL,
        "penpot_exporter_url": EC.DEFAULT_EXPORTER_URL,
        # Two strings that live in two files and have to agree: this one, and
        # the exporter container's own PENPOT_PUBLIC_URI in
        # scripts/sirius/etc/penpot/docker-compose.sirius.yml. Nothing else
        # compares them, and a mismatch is not an error -- it serves a render
        # with the images and fonts stripped out of it. Reported here so the
        # comparison is one command instead of two file reads.
        "penpot_public_uri": EC.DEFAULT_PUBLIC_URI,
        "penpot_internal_uri": EC.DEFAULT_INTERNAL_URI,
        "service_account_configured": bool(EC.DEFAULT_USERNAME and EC.DEFAULT_PASSWORD),
        "view_mount": mount.view_server().status(),
        # A degraded render is otherwise reported only to the request that
        # provoked it (a header) and to the log. Surfacing it here is what
        # makes "this diagram looks wrong" answerable.
        "degraded_renders": mount.view_server().renderer.degraded(),
    }


def force_refresh(file_id: str, page_id: str, board_id: str, *,
                   scale: float = 1.0, swap: list[str] | None = None,
                   crop: str | None = None) -> dict:
    """Render a board now, bypassing the cache's own judgment about whether it
    needs to.

    A warm entry inside its TTL, or one whose freshness probe still says
    "unchanged", is otherwise served as-is -- correct for a browser request,
    wrong for an operator who has a specific reason to believe the cached
    bytes are stale (Penpot's own indexing lagging an edit, a swap/crop bug
    just fixed and worth re-proving against a real board). This evicts the one
    cache slot the request names, then renders through the normal path, so the
    very next ordinary request also sees the fresh result rather than this
    call's answer and the cache disagreeing with each other.
    """
    for label, value in (("file_id", file_id), ("page_id", page_id),
                         ("board_id", board_id)):
        if not R.is_uuid(value):
            raise ValueError(f"{label} {value!r} is not a Penpot UUID")
    spec = R.RenderSpec(scale=float(scale), swaps=R.parse_swaps(swap or ()),
                        crop=(crop or None))
    renderer = mount.view_server().renderer
    key = R.cache_key(file_id, page_id, board_id, spec)
    _evict(renderer.cache, key)
    result = renderer.render(file_id, page_id, board_id, spec)
    return {
        "file_id": file_id, "page_id": page_id, "board_id": board_id,
        "spec": R.describe(spec), "etag": result.etag,
        "bytes": len(result.data), "problems": result.problems,
    }


def _evict(cache, key: tuple[str, str, str, str]) -> None:
    """Drop one cache slot so the next render is a genuine cold miss.

    An operator calling force-refresh means to override the freshness probe,
    which by Penpot's own lights may quite correctly be answering
    "unchanged". :meth:`~awm.penpot_view.view.Cache.invalidate` also retires
    any render still running for that key, so a slow one cannot land on top
    of the replacement's bytes after the fact.
    """
    cache.invalidate(key)


def seed_demo(*, token: str | None = None, team: str | None = None) -> dict:
    """Create the demo Penpot file, or report where the existing one is.

    Not reachable from the internet: ``/svc/penpot-view/fn/*`` is on no
    allow-list in the public edge's policy, so this is a console verb and a
    verb the chain script calls, and nothing else.

    ``token`` is a Penpot session belonging to a real person, which is how
    this authors as one. The render account is a read-only member of the
    shared team on purpose -- see ``scripts/sirius/penpot-team.sh`` -- so
    without a token the seed can read the demo but not create it. ``awm auth
    penpot-session --username <name>`` is where the token comes from, and
    ``scripts/sirius/demo-chain.sh`` is what fetches it.
    """
    with EC.ExporterClient(token=token) as client:
        return D.seed(client, team=team)


def cache_stats() -> dict:
    """Cache health an operator can act on: how full the durability copy on
    disk is, and whether renders are actually being served from it."""
    cache = mount.view_server().renderer.cache
    disk_files = disk_bytes = 0
    if cache.cache_dir.exists():
        for path in cache.cache_dir.rglob("*.svg"):
            try:
                disk_bytes += path.stat().st_size
                disk_files += 1
            except OSError:
                continue
    return {
        "cache_dir": str(cache.cache_dir),
        "ttl_s": cache.ttl,
        "cold_timeout_s": cache.cold_timeout,
        "freshness_enabled": cache.freshness is not None,
        "renders_total": cache.renders,
        "disk_files": disk_files,
        "disk_bytes": disk_bytes,
    }


# --- entry point ---------------------------------------------------------

API_MANIFEST: dict[str, Any] = {
    "functions": [
        {"name": "status"},
        {"name": "force_refresh", "params": [
            {"name": "file_id", "type": "string", "required": True},
            {"name": "page_id", "type": "string", "required": True},
            {"name": "board_id", "type": "string", "required": True},
            {"name": "scale", "type": "number", "required": False},
            {"name": "swap", "type": "array", "required": False},
            {"name": "crop", "type": "string", "required": False},
        ]},
        {"name": "cache_stats"},
        {"name": "seed_demo", "params": [
            {"name": "token", "type": "string", "required": False},
            {"name": "team", "type": "string", "required": False},
        ]},
    ],
    "emitters": [],
    "sessions": [],
}


def _force_refresh_handler(args: dict) -> dict:
    return force_refresh(
        args["file_id"], args["page_id"], args["board_id"],
        scale=args.get("scale", 1.0), swap=args.get("swap"), crop=args.get("crop"))


HANDLERS: dict[str, Any] = {
    "status": lambda args: status(),
    "force_refresh": _force_refresh_handler,
    "cache_stats": lambda args: cache_stats(),
    "seed_demo": lambda args: seed_demo(token=args.get("token"),
                                        team=args.get("team")),
}


def _on_start() -> None:
    """Build the render pipeline and start the view mount's lease loop.

    A task, not an ``await``: ``hold_mount`` never returns, and ``on_start``
    blocking here would stop the adapter's control WS from ever coming up
    (AGENTS.md's ready-ASAP contract -- a slow ``on_start`` is treated as a
    broken service, not a loading one).
    """
    mount.view_server()
    asyncio.create_task(mount.hold_mount())
    log.info("penpot-view: view mount starting at %s", mount.VIEW_PREFIX)


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    adapter = ServiceAdapter("penpot-view", API_MANIFEST, HANDLERS,
                             on_start=_on_start)
    await adapter.run()


if __name__ == "__main__":
    asyncio.run(main())
