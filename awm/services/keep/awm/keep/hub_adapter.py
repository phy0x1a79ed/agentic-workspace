"""Hub adapter for the `keep` service — the Claude Code daemon, kept running.

The verbs join the `reflection` tool beside cx's session verbs, as
``daemon_status``, ``daemon_ensure``, ``daemon_hold`` and ``daemon_release``.
The supervision loop runs `Keeper.reconcile` from a thread, because a launch
shells out to systemd and the gateway reaps a service whose event loop stalls.
A second loop warns the operator, and the sessions that own the biggest
processes, when the node runs low on memory.

Run via ``run.sh`` (which the gateway spawns and respawns):
    python -m awm.keep.hub_adapter
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from awm import config
from awm import gatewayclient
from awm.gatewayclient import ServiceAdapter, spawn_supervised

from awm.keep import daemon, memwatch

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
                "does not come up in time, is held, or cannot run here. "
                "Pass peer=<node> to bring another node's daemon back with no "
                "ssh. When this verb itself does not answer, the node's keep "
                "service is down: `awm services_restart name=keep` on that peer."
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
                "daemon_release or the hold expires. Its sessions keep running "
                "for 20 minutes without the daemon, then die, so a hold lasts "
                "at most that long. Nothing can start, resume or message a "
                "session while it is held."
            ),
            "params": [
                {"name": "reason", "type": "string", "required": True,
                 "description": "Why, shown in daemon_status."},
                {"name": "seconds", "type": "number",
                 "description": "How long the hold lasts. Default 600, at most 1200."},
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
    seconds = float(args.get("seconds") or daemon.HOLD_DEFAULT_S)
    return await asyncio.to_thread(KEEPER.hold, reason, seconds)


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


WATCH = memwatch.MemWatch()


async def _tell_operator(text: str) -> None:
    target = config.operator_dm()
    if target is None:
        log.warning("keep: no operator DM on this node; %s", text)
        return
    account, channel = target
    await gatewayclient.call_maybe_peer(
        gatewayclient.peer_env("AWM_SOCIAL_PEER"), "social", "send",
        {"account": account, "channel": channel, "text": text})


async def memory_tick(now: float | None = None) -> None:
    """Warn once when memory stays low: the operator, then each hog's session."""
    now = time.time() if now is None else now
    total, available = await asyncio.to_thread(memwatch.read_meminfo)
    if not WATCH.tick(now, total, available):
        return
    hogs = await asyncio.to_thread(memwatch.top_hogs)
    node = config.node_name()
    text = memwatch.render(node, total, available, hogs)
    log.warning("keep: %s", text)
    try:
        await _tell_operator(text)
    except Exception as exc:  # noqa: BLE001 — the sessions still hear it
        log.warning("keep: could not tell the operator: %s", exc)
    for hog in WATCH.jobs_to_warn(now, hogs):
        try:
            await gatewayclient.call("reflection", "message", {
                "job": hog.job, "text": memwatch.session_warning(node, hog, available)})
        except Exception as exc:  # noqa: BLE001
            log.warning("keep: could not warn %s: %s", hog.job, exc)


async def _watch_memory() -> None:
    while True:
        try:
            await memory_tick()
        except Exception:  # noqa: BLE001 — a bad tick must not end the loop
            log.exception("keep: memory watch failed")
        await asyncio.sleep(daemon.TICK_S)


SUPERVISION: asyncio.Task | None = None
MEMORY: asyncio.Task | None = None


def _on_start() -> None:
    """Arrange for the loops to run, and return None so startup is not blocked on them."""
    global SUPERVISION, MEMORY
    SUPERVISION = spawn_supervised("keep.reconcile", _keep_daemon_running)
    MEMORY = spawn_supervised("keep.memory", _watch_memory)


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    await ServiceAdapter("keep", API_MANIFEST, HANDLERS, on_start=_on_start).run()


if __name__ == "__main__":
    asyncio.run(main())
