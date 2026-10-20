"""Hub adapter for the `keep` service — the Claude Code daemon, kept running.

The verbs join the `reflection` tool beside cx's session verbs, as
``daemon_status``, ``daemon_ensure``, ``daemon_hold`` and ``daemon_release``.
The supervision loop runs `Keeper.reconcile` from a thread, because a launch
shells out to systemd and the gateway reaps a service whose event loop stalls.

Run via ``run.sh`` (which the gateway spawns and respawns):
    python -m awm.keep.hub_adapter
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from awm.gatewayclient import ServiceAdapter, spawn_supervised

from awm.keep import daemon

log = logging.getLogger("awm.keep.hub_adapter")

KEEPER = daemon.Keeper()

API_MANIFEST: dict[str, Any] = {
    "description": (
        "The Claude Code daemon on this node, kept running in its own systemd "
        "user unit (awm-claude-daemon.service), outside awm and every terminal, "
        "so a deploy or an OOM kill elsewhere never takes the background "
        "sessions with it."
    ),
    "functions": [
        {
            "name": "status",
            "tool": "reflection_daemon_status",
            "description": (
                "Show the Claude Code daemon on this node: pid, origin, unit, "
                "uptime, the unit's restart count and result, why the daemon "
                "last stopped, recent starts, whether it is held, and linger."
            ),
            "params": [],
            "effect": "read",
        },
        {
            "name": "ensure",
            "tool": "reflection_daemon_ensure",
            "description": (
                "Start the Claude Code daemon now if none runs, and wait until "
                "it answers. Returns ok=false with reason=daemon_down when it "
                "does not come up in time, is held, or cannot run here."
            ),
            "params": [
                {"name": "timeout", "type": "number",
                 "description": "Seconds to wait for the daemon. Default 30."},
            ],
            "timeout": 60,
            "effect": "write",
        },
        {
            "name": "hold",
            "tool": "reflection_daemon_hold",
            "description": (
                "Stop the Claude Code daemon and keep it stopped until "
                "daemon_release. Its sessions keep running, but nothing can "
                "start, resume or message one while it is held."
            ),
            "params": [
                {"name": "reason", "type": "string", "required": True,
                 "description": "Why, shown in daemon_status."},
            ],
            "timeout": 60,
            "effect": "write",
        },
        {
            "name": "release",
            "tool": "reflection_daemon_release",
            "description": "End a daemon_hold and start the daemon again.",
            "params": [],
            "timeout": 60,
            "effect": "write",
        },
    ],
    "emitters": [],
    "sessions": [],
}


async def _status(args: dict[str, Any]) -> dict[str, Any]:
    return await asyncio.to_thread(KEEPER.status)


async def _ensure(args: dict[str, Any]) -> dict[str, Any]:
    timeout = float(args.get("timeout") or daemon.ENSURE_TIMEOUT_S)
    return await asyncio.to_thread(KEEPER.ensure, min(timeout, 55.0))


async def _hold(args: dict[str, Any]) -> dict[str, Any]:
    reason = str(args.get("reason") or "").strip()
    if not reason:
        return {"ok": False, "error": "a hold needs a reason"}
    return await asyncio.to_thread(KEEPER.hold, reason)


async def _release(args: dict[str, Any]) -> dict[str, Any]:
    return await asyncio.to_thread(KEEPER.release)


HANDLERS: dict[str, Any] = {
    "status": _status,
    "ensure": _ensure,
    "hold": _hold,
    "release": _release,
}


async def _keep_daemon_running() -> None:
    """Reconcile, and keep reconciling. Never returns."""
    while True:
        try:
            await asyncio.to_thread(KEEPER.reconcile)
        except Exception:  # noqa: BLE001 — a bad tick must not end the loop
            log.exception("keep: reconcile failed")
        await asyncio.sleep(daemon.TICK_S)


SUPERVISION: asyncio.Task | None = None


def _on_start() -> None:
    """Arrange for the loop to run, and return None so startup is not blocked on it."""
    global SUPERVISION
    SUPERVISION = spawn_supervised("keep.reconcile", _keep_daemon_running)


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    await ServiceAdapter("keep", API_MANIFEST, HANDLERS, on_start=_on_start).run()


if __name__ == "__main__":
    asyncio.run(main())
