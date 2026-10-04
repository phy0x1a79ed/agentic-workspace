"""Hub adapter for the `cx` service — the warm start for Claude Code.

Keeps one background Claude Code session idling so that `cx` in a terminal
attaches to a renderer that has already started, instead of paying a cold
launch. The session is seeded in a neutral directory and moved to the caller's
directory at claim time, so the pool is not keyed by directory and the first
`cx` in a project is as fast as the hundredth.

Every function carries an explicit ``tool`` name under a ``cx_`` prefix, which
is what decides the domain: the gateway folds the MCP surface by splitting the
projected name on its first underscore. So the surface is ``awm cx status`` and
``mcp__awm__cx {verb:"status"}``.

Run via ``run.sh`` (which the gateway spawns and respawns):
    python -m awm.cx.hub_adapter
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from awm.gatewayclient import ServiceAdapter, spawn_supervised

from awm.cx import claim, pool, reconcile, remove, seed

log = logging.getLogger("awm.cx.hub_adapter")

LOOP = reconcile.Loop()

API_MANIFEST: dict[str, Any] = {
    "description": (
        "The warm start for Claude Code. A background session is kept idling "
        "so the `cx` command in a terminal attaches to an already-painted "
        "renderer. Read `status` to see what is being held; the other verbs "
        "are for operating the pool, and `cx` itself is the everyday surface."
    ),
    "functions": [
        {
            "name": "status",
            "tool": "cx_status",
            "description": (
                "Report the pool: every session this service owns with its "
                "age, whether it is claimable and why not, whether a Claude "
                "Code daemon is running at all, the binary version the pool "
                "seeds against, whether this process holds the reconcile lock "
                "and when its last tick ran."
            ),
            "params": [],
        },
        {
            "name": "claim",
            "tool": "cx_claim",
            "description": (
                "Take the warm session, move it to `cwd`, and return its id for "
                "`claude attach`. An empty id means there was nothing warm, "
                "which the caller answers with an ordinary cold launch. This is "
                "what the `cx` command calls; a person has no reason to."
            ),
            "params": [
                {"name": "cwd", "type": "string", "required": True,
                 "description": "The directory to move the session to."},
            ],
            "timeout": 9,
        },
        {
            "name": "seed",
            "tool": "cx_seed",
            "description": (
                "Start one warm session now and return its id. Refuses, with "
                "the reason, when no Claude Code daemon is already running: "
                "seeding then would put every background session on this node "
                "inside awm's control group, where the next deploy kills it. "
                "The reconcile loop calls this on its own; reach for it by "
                "hand only to see what a seed does or why one is refused."
            ),
            "params": [],
            "timeout": 60,
        },
        {
            "name": "remove",
            "tool": "cx_remove",
            "description": (
                "List the sessions this service may delete, with the reason "
                "each one qualifies. Deletes nothing unless `apply` is true. "
                "A session that has been renamed, prompted, or claimed and is "
                "still alive never appears here: once somebody has taken a "
                "session it is theirs, and the pool waits for the daemon's own "
                "retirement instead."
            ),
            "params": [
                {"name": "apply", "type": "boolean",
                 "description": "Carry out the plan (default false)."},
            ],
            "timeout": 120,
        },
    ],
    "emitters": [],
    "sessions": [],
}

def _on_start() -> None:
    """Start the reconcile loop, unless this process must not own the pool.

    Through `spawn_supervised` so a loop that dies on its first line is logged
    and respawned, rather than leaving the service registered and healthy with
    the warm session silently absent.
    """
    why = LOOP.enabled()
    if why:
        log.info("cx: not reconciling — %s", why)
        return
    spawn_supervised("cx.reconcile", LOOP.run)


async def _seed(args: dict[str, Any]) -> dict[str, Any]:
    try:
        return {"session": await seed.seed_one()}
    except seed.Refused as exc:
        return {"session": None, "refused": str(exc)}


async def _remove(args: dict[str, Any]) -> dict[str, Any]:
    if args.get("apply"):
        return {"applied": await remove.apply()}
    return {"plan": remove.plan()}


async def _claim(args: dict[str, Any]) -> dict[str, Any]:
    out = await claim.claim(args.get("cwd") or "")
    if out.get("session"):
        # Seed the replacement while the caller is still starting up, rather
        # than up to a tick later.
        LOOP.kick()
    return out


HANDLERS: dict[str, Any] = {
    "status": lambda args: pool.status(LOOP),
    "claim": _claim,
    "seed": _seed,
    "remove": _remove,
}


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    await ServiceAdapter("cx", API_MANIFEST, HANDLERS,
                         on_start=_on_start).run()


if __name__ == "__main__":
    asyncio.run(main())
