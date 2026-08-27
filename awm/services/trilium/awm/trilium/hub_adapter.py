"""Hub adapter for the trilium service — one knowledge base per person.

Registers with the gateway on the shared `ServiceAdapter` loop (register →
ready → serve → reconnect), so the fleet of Trilium servers is a service like
any other: visible in `awm services list`, health as a verb, and reachable from
any node rather than being node processes somebody started by hand.

Three things live under this one supervised process:

  - one Trilium server per user, on loopback — see `server`,
  - one TLS front per user, behind awm's edge session — see `front`,
  - the discovery that decides who "per user" means — see `instances`.

All of them die with this process. Trilium persists everything to its data
directory, so an awm deploy costs a browser reload rather than any content.

Run via `run.sh` (which the gateway spawns and respawns):
    python -m awm.trilium.hub_adapter
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from awm.gatewayclient import ServiceAdapter, spawn_supervised

from awm.trilium import front, instances, server

log = logging.getLogger("awm.trilium.hub_adapter")

FLEET = server.Fleet()

#: Every function carries an explicit `tool` name under a `trilium_` prefix,
#: which is what decides the domain this service appears as: the gateway folds
#: the MCP surface by splitting the projected name on its **first** underscore.
#: So the surface is `awm trilium status` and `mcp__awm__trilium {verb:"status"}`.
API_MANIFEST: dict[str, Any] = {
    "functions": [
        {
            "name": "status",
            "tool": "trilium_status",
            "description": (
                "Report every Trilium instance on this node: which users have "
                "a scope, whether each server is running and at what pid/port, "
                "whether it is actually listening, which rolling backups that "
                "user has, each mesh front's TLS state with the URL to open, "
                "and which server bundle is being served — the fork worktree "
                "or the published tarball — with whether the build matches the "
                "revision on disk."
            ),
            "params": [],
        },
        {
            "name": "users",
            "tool": "trilium_users",
            "description": (
                "The users this node serves and the ports they own. A user "
                "exists because a scope userdata/trilium/<user> exists on "
                "disk, so adding a person is `awm scope create` and nothing "
                "else. Ports are allocated once and remembered, so adding a "
                "user never moves an existing one's URL."
            ),
            "params": [],
        },
        {
            "name": "start",
            "tool": "trilium_start",
            "description": (
                "Start a user's server, waiting until it binds. With no user, "
                "start every user's. Idempotent."
            ),
            "params": [
                {"name": "user", "type": "string",
                 "description": "User to start. Omit for all."},
            ],
            "timeout": 300,
        },
        {
            "name": "stop",
            "tool": "trilium_stop",
            "description": (
                "Stop a user's server, ending live browser connections. "
                "Content is on disk, so this loses no notes. With no user, "
                "stop every user's. Idempotent. The supervision loop respawns "
                "a stopped server on its next pass — use this to bounce one, "
                "not to keep it down."
            ),
            "params": [
                {"name": "user", "type": "string",
                 "description": "User to stop. Omit for all."},
            ],
            "timeout": 120,
        },
        {
            "name": "restart",
            "tool": "trilium_restart",
            "description": (
                "Stop a user's server and start it again — e.g. to pick up a "
                "rebuilt bundle or an edited config.ini, both read at startup."
            ),
            "params": [
                {"name": "user", "type": "string",
                 "description": "User to restart. Omit for all."},
            ],
            "timeout": 300,
        },
        {
            "name": "url",
            "tool": "trilium_url",
            "description": (
                "The mesh URL that opens a user's Trilium. Behind awm's edge "
                "session, and then behind that user's own Trilium login — two "
                "gates, because the edge session is one shared password and "
                "only the second one says which person."
            ),
            "params": [
                {"name": "user", "type": "string", "required": True,
                 "description": "Whose instance to link to."},
            ],
        },
        {
            "name": "logs",
            "tool": "trilium_logs",
            "description": "Tail one user's server stdout/stderr log.",
            "params": [
                {"name": "user", "type": "string", "required": True,
                 "description": "Whose log to read."},
                {"name": "tail", "type": "number",
                 "description": "Lines to return (default 200)."},
            ],
        },
    ],
    "emitters": [],
    "sessions": [],
}


# -- handlers ---------------------------------------------------------------
#
# Every handler hops to a worker thread: they spawn processes, signal groups
# and poll sockets, and any of those on the event loop would stall the control
# WS and have the gateway take this service for dead.


def _resolve(user: str | None) -> list[server.Child]:
    """The children a verb addresses. Named users must exist; absent means all."""
    if not user:
        return FLEET.children()
    child = FLEET.get(user)
    if child is None:
        known = ", ".join(sorted(c.inst.user for c in FLEET.children())) or "none"
        raise KeyError(f"no Trilium instance for user {user!r} (known: {known})")
    return [child]


async def _h_status(args: dict) -> dict:
    return {
        "instances": await asyncio.to_thread(FLEET.snapshot),
        "fronts": front.status(),
        "source": await asyncio.to_thread(instances.source_state),
    }


async def _h_users(args: dict) -> dict:
    def _read() -> dict:
        FLEET.sync()
        return {
            "users": [
                {"user": i.user, "slot": i.slot,
                 "front_port": i.front_port, "upstream_port": i.upstream_port,
                 "scope": str(i.scope)}
                for i in instances.instances()
            ],
            "userdata_dir": str(instances.USERDATA_DIR),
            "max_users": instances.MAX_USERS,
        }
    return await asyncio.to_thread(_read)


async def _h_start(args: dict) -> dict:
    user = (args.get("user") or "").strip() or None
    if user is None:
        return {"results": await asyncio.to_thread(FLEET.start_all)}
    children = await asyncio.to_thread(_resolve, user)
    return {"results": [await asyncio.to_thread(c.start) for c in children]}


async def _h_stop(args: dict) -> dict:
    user = (args.get("user") or "").strip() or None
    children = await asyncio.to_thread(_resolve, user)
    return {"results": [await asyncio.to_thread(c.stop) for c in children]}


async def _h_restart(args: dict) -> dict:
    user = (args.get("user") or "").strip() or None
    children = await asyncio.to_thread(_resolve, user)
    return {"results": [await asyncio.to_thread(c.restart) for c in children]}


async def _h_url(args: dict) -> dict:
    user = (args.get("user") or "").strip()
    inst = await asyncio.to_thread(instances.instance, user)
    if inst is None:
        raise KeyError(f"no Trilium instance for user {user!r}")
    return {"user": user, "url": front.origin(inst),
            "note": "requires an awm session, then this user's Trilium login"}


async def _h_logs(args: dict) -> dict:
    user = (args.get("user") or "").strip()
    tail = int(args.get("tail") or 200)
    children = await asyncio.to_thread(_resolve, user)
    child = children[0]
    return {"user": user, "tail": tail,
            "log": await asyncio.to_thread(child.logs, tail)}


HANDLERS = {
    "status": _h_status,
    "users": _h_users,
    "start": _h_start,
    "stop": _h_stop,
    "restart": _h_restart,
    "url": _h_url,
    "logs": _h_logs,
}


# -- the supervision loop ---------------------------------------------------


async def _health_loop() -> None:
    """Respawn dead children and raise fronts for new users. Never exits.

    Watches process liveness rather than an HTTP probe: a slow probe while
    Trilium is importing a large attachment is not evidence of death, and
    respawning on it would cut the work it was mistaking for a hang.
    """
    log.info("trilium: supervision loop started (interval=%ss)",
             instances.HEALTH_INTERVAL_S)
    while True:
        try:
            await asyncio.sleep(instances.HEALTH_INTERVAL_S)
            for res in await asyncio.to_thread(FLEET.reconcile):
                if res.get("action") == "respawned":
                    log.warning("trilium[%s]: respawned (previous exit %s)",
                                res.get("user"), res.get("previous_exit"))
            for user in await asyncio.to_thread(front.sync):
                log.info("trilium[%s]: front raised", user)
        except Exception:  # noqa: BLE001 — never let the loop die
            # CancelledError is a BaseException and so passes through, which is
            # what the supervisor above wants: a *return* from here would read
            # as a defect and be respawned, but a cancellation is a shutdown.
            log.exception("trilium: supervision pass failed")


async def _on_start() -> None:
    """Start every user's server, raise their fronts, then loop.

    No failure here is fatal. The service still registers, so `status` can
    report *why* it is broken, and the loop keeps retrying.
    """
    if instances.entry_point() is None:
        log.warning("trilium: no server bundle at %s or %s — run install.sh; "
                    "the service will register and report this via status",
                    instances.FORK_ENTRY, instances.TARBALL_ENTRY)
    else:
        try:
            for res in await asyncio.to_thread(FLEET.start_all):
                log.info("trilium[%s]: %s pid=%s listening=%s",
                         res.get("user"), res.get("action"), res.get("pid"),
                         res.get("listening"))
        except Exception:  # noqa: BLE001
            log.exception("trilium: initial start failed; the loop will retry")

    try:
        await asyncio.to_thread(front.sync)
    except Exception:  # noqa: BLE001 — the servers are still usable on loopback
        log.exception("trilium: mesh fronts failed to start")

    # A dead supervision loop looks exactly like a fleet that has not crashed,
    # so it is spawned supervised rather than as a bare task nobody reads.
    spawn_supervised("trilium:health", _health_loop)


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    await ServiceAdapter("trilium", API_MANIFEST, HANDLERS,
                         on_start=_on_start).run()


if __name__ == "__main__":
    asyncio.run(main())
