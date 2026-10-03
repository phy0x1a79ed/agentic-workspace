"""Hub adapter for the penpot service — supervises the Penpot compose stack.

Registers with the gateway on the shared `ServiceAdapter` loop (register →
ready → serve → reconnect), so the stack is a service like any other:
visible in `awm services list`, health as a verb, reachable from any node
rather than five containers somebody started by hand.

**This module carries no HTTP or WebSocket traffic.** It supervises the
stack; a separate httpsfront wiring (a different task, a different agent)
proxies `/penpot` to the running frontend container. Mixing the two into one
`kind="service"` mount is exactly the shape Trilium's own `server.py`/edge
split avoids, for the same reason: a control-plane RPC channel and a
browser's live traffic have different failure modes, and coupling them means
a slow render blocks a status check (or vice versa).

**Penpot owns its own users.** Unlike a `userroot`-partitioned service, there
is no per-caller state here — one shared compose stack, one shared Postgres
where Penpot keeps its own accounts and teams. See `INSTALL.md`.

Run via `run.sh` (which the gateway spawns and respawns):
    python -m awm.penpot.hub_adapter
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from awm.gatewayclient import ServiceAdapter, spawn_supervised

from awm.penpot import stack

log = logging.getLogger("awm.penpot.hub_adapter")

STACK = stack.STACK

#: Every function carries an explicit `tool` name under a `penpot_` prefix,
#: which is what decides the domain this service appears as: the gateway
#: folds the MCP surface by splitting the projected name on its **first**
#: underscore. So the surface is `awm penpot status` and
#: `mcp__awm__penpot {verb:"status"}`.
API_MANIFEST: dict[str, Any] = {
    "functions": [
        {
            "name": "status",
            "tool": "penpot_status",
            "description": (
                "Whether the Penpot stack is up, and which containers (if "
                "any) are missing or unhealthy. A caller arriving through "
                "the edge gets the readable half; the compose directory and "
                "per-container detail are for the console."
            ),
            "params": [],
        },
        {
            "name": "start",
            "tool": "penpot_start",
            "description": (
                "Bring the compose stack up (`docker compose up -d`) if it "
                "is not already. Operator only."
            ),
            "params": [],
            "timeout": 300,
        },
        {
            "name": "stop",
            "tool": "penpot_stop",
            "description": (
                "Take the compose stack down (`docker compose down`). "
                "Operator only — this takes Penpot away from everyone using it."
            ),
            "params": [],
            "timeout": 300,
        },
        {
            "name": "restart",
            "tool": "penpot_restart",
            "description": "Stop then start the compose stack. Operator only.",
            "params": [],
            "timeout": 300,
        },
        {
            "name": "logs",
            "tool": "penpot_logs",
            "description": (
                "Tail `docker compose logs` for the stack, or one container. "
                "Operator only: it carries stack traces and internal addresses."
            ),
            "params": [
                {"name": "service", "type": "string",
                 "description": "One service name, e.g. penpot-backend. Default: all."},
                {"name": "tail", "type": "number",
                 "description": "Lines from the end, per container. Default 200."},
            ],
        },
        {
            "name": "url",
            "tool": "penpot_url",
            "description": (
                "Where Penpot is served. A path, not a URL: it is on the "
                "same origin as the page asking, behind the same session."
            ),
            "params": [],
        },
    ],
    "emitters": [],
    "sessions": [],
}


# -- who may call what --------------------------------------------------


def _operator_only(as_: str | None, verb: str) -> None:
    """Refuse a verb that arrived through an edge listener.

    The stack is shared, so every write verb is one person's button acting
    on everyone's session — `stop`/`restart` drop every open editor's
    websocket at once. The split mirrors Trilium's `_operator_only` exactly,
    including the discriminator: `httpsfront` overwrites `X-Awm-As` on every
    request it forwards and never forwards an empty one, so an absent
    identity means the call did not cross an edge — it came from `/invoke`
    on loopback, the host's own CLI.

    Deliberately not `userroot.wrap_handlers`: this stack has no per-caller
    store to resolve, and under `AWM_USER_ROOT_STRICT` that helper raises for
    exactly the caller this function needs to admit.
    """
    if as_ is not None:
        raise PermissionError(
            f"{verb} acts on the shared Penpot stack and is an operator "
            f"verb: run `awm penpot {verb}` on the host")


# -- handlers -------------------------------------------------------------
#
# Every handler hops to a worker thread: they shell out to `docker compose`
# and block on its exit, and any of those on the event loop would stall the
# control WS and have the gateway take this service for dead.


async def _h_status(args: dict, as_: str | None = None) -> dict:
    verbose = as_ is None
    return await asyncio.to_thread(STACK.status, verbose=verbose)


async def _h_start(args: dict, as_: str | None = None) -> dict:
    _operator_only(as_, "start")
    return await asyncio.to_thread(STACK.start)


async def _h_stop(args: dict, as_: str | None = None) -> dict:
    """Stop the stack and *hold* it stopped.

    `hold=True` is what makes this verb mean anything. Without it the
    supervision loop below sees a cleanly-downed stack on its next pass and
    brings it straight back — verified live: the stack was up again 9 seconds
    after a `stop` returned `stack_state: stopped`. An operator stops the
    stack for a reason (freeing memory on a small host, taking it out of the
    way of an image rebuild), and a supervisor that overrules that within one
    interval makes the verb a 20-second outage rather than a stop. `start`
    releases the hold, so this is not a state anyone can get wedged in.
    """
    _operator_only(as_, "stop")
    return await asyncio.to_thread(lambda: STACK.stop(hold=True))


async def _h_restart(args: dict, as_: str | None = None) -> dict:
    _operator_only(as_, "restart")
    return await asyncio.to_thread(STACK.restart)


async def _h_logs(args: dict, as_: str | None = None) -> dict:
    _operator_only(as_, "logs")
    service = (args.get("service") or "").strip() or None
    tail = int(args.get("tail") or 200)
    log_text = await asyncio.to_thread(STACK.logs, service=service, tail=tail)
    return {"service": service, "tail": tail, "log": log_text}


async def _h_url(args: dict, as_: str | None = None) -> dict:
    """Penpot's path, not a URL.

    There is nothing to compute: Penpot is on the same origin as whatever
    page is rendering the link, reached through the same session. A host and
    a port here would be a guess, and a hardcoded one guesses wrong on any
    node with more than one address — same reasoning as Trilium's `_h_url`.
    """
    return {"path": "/penpot"}


HANDLERS = {
    "status": _h_status,
    "start": _h_start,
    "stop": _h_stop,
    "restart": _h_restart,
    "logs": _h_logs,
    "url": _h_url,
}


# -- the supervision loop --------------------------------------------------


async def _health_loop() -> None:
    """Watch the stack and log transitions. Never exits.

    Does not respawn anything itself. Each container's own `restart:` policy
    is what brings a crashed container back — duplicating that here would
    just be two supervisors racing over the same containers. The one thing
    this loop nudges is the whole stack having been cleanly `down`ed: an
    idempotent `up -d` (skipped while `_held`) is enough to bring it back,
    the same as a fresh `start`.
    """
    log.info("penpot: supervision loop started (interval=%ss)",
             stack.HEALTH_INTERVAL_S)
    while True:
        try:
            await asyncio.sleep(stack.HEALTH_INTERVAL_S)
            state = await asyncio.to_thread(STACK.reconcile)
            action = state["stack_state"]
            if action == "unhealthy":
                log.warning("penpot: unhealthy — missing=%s unhealthy=%s",
                            state["missing"], state["unhealthy"])
            elif action == "stopped" and not STACK.held:
                log.warning("penpot: stack is down — bringing it back up")
                try:
                    await asyncio.to_thread(STACK.start)
                except Exception:  # noqa: BLE001 — reportable via status, not fatal
                    log.exception("penpot: could not restart the stopped stack")
        except Exception:  # noqa: BLE001 — never let the loop die
            # CancelledError is a BaseException and so passes through, which
            # is what the supervisor above wants: a *return* from here would
            # read as a defect and be respawned, but a cancellation is a
            # shutdown.
            log.exception("penpot: supervision pass failed")


async def _on_start() -> None:
    """Bring the stack up if it is not already, then start the health loop.

    No failure here is fatal. The service still registers, so `status` can
    report *why* it is broken, and the loop keeps retrying.
    """
    if not STACK.config.exists:
        log.warning("penpot: no compose file at %s — the service will "
                    "register and report this via status",
                    STACK.config.compose_dir / STACK.config.compose_file)
    elif STACK.held:
        # The gateway respawns this service on any crash, deploy or restart.
        # Starting unconditionally here would undo an operator's deliberate
        # stop at exactly the moment nobody is watching, which is the failure
        # `hold` exists to prevent — so honour it and say so.
        log.warning("penpot: stack is held stopped (%s); not starting it. "
                    "Call the start verb to release the hold.",
                    STACK.hold_file)
    else:
        try:
            res = await asyncio.to_thread(STACK.start)
            log.info("penpot: %s stack_state=%s", res.get("action"),
                     res.get("stack_state"))
        except Exception:  # noqa: BLE001
            log.exception("penpot: initial start failed; the loop will retry")

    # A dead supervision loop looks exactly like a stack that has not
    # crashed, so it is spawned supervised rather than as a bare task nobody
    # reads.
    spawn_supervised("penpot:health", _health_loop)


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    await ServiceAdapter("penpot", API_MANIFEST, HANDLERS,
                         on_start=_on_start).run()


if __name__ == "__main__":
    asyncio.run(main())
