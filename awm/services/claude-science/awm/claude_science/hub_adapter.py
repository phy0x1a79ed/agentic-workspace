"""Hub adapter for the claude-science service — the Claude Science workbench.

Registers with the gateway on the shared ``ServiceAdapter`` loop (register →
ready → serve → reconnect), so a workbench that was previously three hand-rolled
systemd units and a forked copy of awm's own reverse proxy becomes a service
like any other: visible in ``awm services list``, health as a verb, and
reachable from any node in the fleet.

Like ``virtmic``, this service's real job is owning something it did not write.
Unlike every other one, it owns it *at arm's length*: the workbench daemon is
adopted rather than parented, so an awm deploy cannot interrupt a conversation.
See ``daemon`` for that argument in full.

Three things live under this one supervised process, and all three are things
the gateway registration does not carry:

  - the workbench daemon (adopted; outlives us on purpose),
  - two TLS fronts on the mesh, one per workbench origin, behind awm's edge
    session — see ``front``,
  - an MCP bridge handing the workbench a curated slice of awm's verbs — see
    ``mcp_bridge``.

The fronts and the bridge die with this process, which is right: they are pure
plumbing and cost nothing to rebuild. The daemon does not, which is also right.

Run via ``run.sh`` (which the gateway spawns and respawns):
    python -m awm.claude_science.hub_adapter
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from awm.gatewayclient import ServiceAdapter, spawn_supervised

from awm.claude_science import daemon, front, mcp_bridge

log = logging.getLogger("awm.claude_science.hub_adapter")

SUPERVISOR = daemon.Supervisor(
    main_front_port=front.PORT,
    sandbox_front_port=front.SANDBOX_PORT,
)


#: Every function carries an explicit ``tool`` name under a ``science_`` prefix,
#: which is what decides the domain this service appears as: the gateway folds
#: the MCP surface by splitting the projected name on its **first** underscore.
#: The obvious ``claude_science_status`` therefore lands as domain ``claude`` /
#: verb ``science_status`` — one token, or the service's name gets cut in half.
#: So the surface is ``awm science status`` and ``mcp__awm__science
#: {verb:"status"}``, while the internal ``name`` stays short for RPC dispatch.
API_MANIFEST: dict[str, Any] = {
    "functions": [
        {
            "name": "status",
            "tool": "science_status",
            "description": (
                "Report the Claude Science workbench: whether the daemon is "
                "running and at what pid/version/port, whether this service "
                "adopted it or started it, the three install steps (binary, "
                "provisioned data dir, daemon) named separately, both mesh TLS "
                "fronts' listener + TLS state, and the MCP bridge's mount and "
                "allowlist size."
            ),
            "params": [],
        },
        {
            "name": "start",
            "tool": "science_start",
            "description": (
                "Adopt the running workbench daemon, or launch one detached if "
                "none is running. Idempotent. The daemon deliberately outlives "
                "this service, so this is safe to call at any time."
            ),
            "params": [],
            "timeout": 180,
        },
        {
            "name": "stop",
            "tool": "science_stop",
            "description": (
                "Stop the workbench daemon through its own socket, ending live "
                "conversations. Idempotent. Stopping the *service* does not do "
                "this — you have to mean it."
            ),
            "params": [],
            "timeout": 180,
        },
        {
            "name": "restart",
            "tool": "science_restart",
            "description": (
                "Stop the workbench daemon and start it again — e.g. to pick up "
                "an update that has been staged. Interrupts live conversations."
            ),
            "params": [],
            "timeout": 300,
        },
        {
            "name": "logs",
            "tool": "science_logs",
            "description": "Tail the workbench daemon's log.",
            "params": [
                {"name": "tail", "type": "number",
                 "description": "Lines to return (default 200)."},
            ],
        },
        {
            "name": "url",
            "tool": "science_url",
            "description": (
                "Mint a fresh single-use sign-in link for the workbench, valid "
                "about three minutes. The loopback form the binary prints — "
                "useful over an SSH tunnel. For a browser already holding an "
                "awm session, prefer signin_url."
            ),
            "params": [],
        },
        {
            "name": "signin_url",
            "tool": "science_signin_url",
            "description": (
                "The mesh URL that signs a browser into the workbench in one "
                "click. Behind awm's edge session, so it is only usable by "
                "someone who could log into awm in the first place."
            ),
            "params": [],
        },
        {
            "name": "update_check",
            "tool": "science_update_check",
            "description": (
                "Ask the binary whether a newer build is available. Reports "
                "only; installing is a deliberate act (`claude-science update`)."
            ),
            "params": [],
            "timeout": 180,
        },
    ],
    "emitters": [],
    "sessions": [],
}


# -- handlers ---------------------------------------------------------------
#
# Every handler hops to a worker thread: they spawn processes and wait on
# subprocesses, and either of those on the event loop would stall the control
# WS and have the gateway take this service for dead.


async def _h_status(args: dict) -> dict:
    snap = await asyncio.to_thread(SUPERVISOR.snapshot)
    install = await asyncio.to_thread(SUPERVISOR.install_state)
    return {
        "daemon": snap,
        "install": install,
        "fronts": front.status(),
        "mcp_bridge": mcp_bridge.status(),
        "origins": daemon.allowed_origins(front.PORT),
    }


async def _h_start(args: dict) -> dict:
    return await asyncio.to_thread(SUPERVISOR.start)


async def _h_stop(args: dict) -> dict:
    return await asyncio.to_thread(SUPERVISOR.stop)


async def _h_restart(args: dict) -> dict:
    return await asyncio.to_thread(SUPERVISOR.restart)


async def _h_logs(args: dict) -> dict:
    tail = int(args.get("tail") or 200)
    return {"tail": tail, "log": await asyncio.to_thread(SUPERVISOR.logs, tail)}


async def _h_url(args: dict) -> dict:
    return {"url": await asyncio.to_thread(SUPERVISOR.login_url)}


async def _h_signin_url(args: dict) -> dict:
    return {"url": f"{daemon.front_origin(front.PORT)}/_signin",
            "note": "requires an awm session; mints a workbench nonce server-side"}


async def _h_update_check(args: dict) -> dict:
    return await asyncio.to_thread(SUPERVISOR.update_check)


HANDLERS = {
    "status": _h_status,
    "start": _h_start,
    "stop": _h_stop,
    "restart": _h_restart,
    "logs": _h_logs,
    "url": _h_url,
    "signin_url": _h_signin_url,
    "update_check": _h_update_check,
}


# -- the supervision loop ---------------------------------------------------


async def _health_loop() -> None:
    """Restart the workbench daemon whenever it has actually died. Never exits.

    Watches process liveness rather than an HTTP probe: a slow probe during an
    analysis is not evidence of death, and respawning on it would interrupt the
    very work this service is arranged to protect.
    """
    log.info("claude-science: supervision loop started (interval=%ss)",
             daemon.HEALTH_INTERVAL_S)
    while True:
        try:
            await asyncio.sleep(daemon.HEALTH_INTERVAL_S)
            res = await asyncio.to_thread(SUPERVISOR.reconcile)
            if res.get("action") == "respawned":
                log.warning("claude-science: workbench daemon respawned")
        except Exception:  # noqa: BLE001 — never let the loop die
            # CancelledError is a BaseException and so passes through, which is
            # what the supervisor above wants: a *return* from here would read
            # as a defect and be respawned, but a cancellation is a shutdown.
            log.exception("claude-science: supervision pass failed")


async def _on_start() -> None:
    """Adopt-or-start the daemon, raise the fronts and the bridge, then loop.

    No failure here is fatal. The service still registers, so ``status`` can
    report *why* it is broken — which is the visibility the hand-managed units
    never had — and the loop keeps retrying the daemon.
    """
    front.attach(SUPERVISOR)

    if not daemon.installed():
        log.warning("claude-science: binary missing at %s — run install.sh; the "
                    "service will register and report this via status", daemon.BIN)
    else:
        try:
            snap = await asyncio.to_thread(SUPERVISOR.start)
            log.info("claude-science: daemon %s pid=%s version=%s port=%s",
                     "adopted" if snap.get("adopted") else "started",
                     snap.get("pid"), snap.get("version"), snap.get("port"))
        except Exception:  # noqa: BLE001
            log.exception("claude-science: initial adopt/start failed; "
                          "the loop will retry")

    try:
        front.start()
    except Exception:  # noqa: BLE001 — the workbench is still usable on loopback
        log.exception("claude-science: mesh fronts failed to start")

    # Both are long-lived and both fail silently if left to a bare create_task
    # whose handle nobody reads: a bridge that died on its first line looks
    # exactly like one nobody has called yet, and a dead supervision loop looks
    # exactly like a workbench that has not crashed. spawn_supervised logs at
    # ERROR and respawns, so "absent" cannot masquerade as "quiet".
    spawn_supervised("claude-science:mcp-bridge", mcp_bridge.serve_and_mount)
    spawn_supervised("claude-science:health", _health_loop)


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    await ServiceAdapter(
        "claude-science", API_MANIFEST, HANDLERS, on_start=_on_start,
    ).run()


if __name__ == "__main__":
    asyncio.run(main())
