"""Hub adapter for the claude-view service — fleet observability.

Registers with the gateway on the shared ``awm.gatewayclient.ServiceAdapter``
loop (register → ready → serve → reconnect), so the upstream claude-view server
gets the lifecycle every other awm backend has: started on boot, supervised,
respawned on crash, drained on teardown, visible in ``awm services list``.

Like ``virtmic``, this service's real job is owning something it did not write —
there, PulseAudio; here, a Rust binary that indexes ``~/.claude/projects`` and
serves a dashboard. And like ``httpsfront``, it owns a listener the gateway
registration does not carry: a TLS front on the mesh, because the gateway is
loopback-only and the binary has no auth of its own.

Both of those die with this process, which is the point — one supervised
lifetime covering the whole stack, rather than a hand-managed daemon plus a
hand-managed proxy that drift apart.

What this deliberately does NOT do is proxy claude-view's traffic through the
gateway. Its SPA makes same-origin absolute calls (``/api/...``) with no
base-path support, so serving it under a ``/ui/<name>`` prefix would break every
request. The dedicated front on its own port is what avoids that, and it is why
this is not a ``kind=url`` registration.

Run via ``run.sh`` (which the gateway spawns and respawns):
    python -m awm.claude_view.hub_adapter

Functions:
  - status  (tool ``claude-view_status``) — binary version and install state,
            child pid/uptime/restarts, upstream health, index size, and the
            mesh front's listener + TLS state.
  - restart (tool ``claude-view_restart``) — stop and respawn the upstream
            server (e.g. after a version bump). Idempotent.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
from typing import Any

from awm.gatewayclient import ServiceAdapter

from awm.claude_view import binary, front

log = logging.getLogger("awm.claude_view.hub_adapter")

SUPERVISOR = binary.Supervisor()


def _install_sigterm_cleanup() -> None:
    """Run ``Supervisor.stop`` on SIGTERM, not only on a clean exit.

    ``Supervisor`` registers its cleanup — kill the child's process group,
    strip our hooks out of ``settings.json`` — with ``atexit``. That covers a
    normal exit and a Ctrl-C (SIGINT raises ``KeyboardInterrupt``, which
    unwinds and runs ``atexit``), which is what standalone testing exercised.

    It does **not** cover SIGTERM, and SIGTERM is exactly how the gateway stops
    a service: ``awm services stop`` / ``disable`` / ``restart`` all go through
    ``kill_pid_group``, which ``SIGTERM``s the process group. Python's default
    SIGTERM action terminates the process **without** running ``atexit`` — so
    without this handler every gateway-mediated stop leaves our 25 hooks behind
    in the fleet-wide ``settings.json``, each one curling at a now-dead port on
    every tool call of every agent on the host, forever. That is silent and
    fleet-wide, the worst shape of failure this service has.

    The child is separately covered by ``PR_SET_PDEATHSIG`` (it dies with us no
    matter how we die), but nothing else strips the hooks, so this handler's
    real job is that removal. ``stop`` is idempotent and never raises, so a
    later ``atexit`` firing too is harmless. ``os._exit`` after it skips a
    second unwind we have already done by hand.
    """
    def _graceful(signum: int, _frame: object) -> None:
        log.info("claude-view: SIGTERM — cleaning up before exit")
        try:
            SUPERVISOR.stop()
        finally:
            os._exit(0)

    signal.signal(signal.SIGTERM, _graceful)


API_MANIFEST: dict[str, Any] = {
    "functions": [
        {
            "name": "status",
            "description": (
                "Report claude-view health: pinned binary version and whether "
                "it is installed, the supervised child's pid/uptime/restart "
                "count, an /api/health probe of the upstream server, the "
                "on-disk index size, and the mesh HTTPS front's port and TLS "
                "state."
            ),
            "params": [],
        },
        {
            "name": "restart",
            "description": (
                "Stop the upstream claude-view server and start it again. Use "
                "after staging a new binary. Idempotent; the supervision loop "
                "would recover it anyway."
            ),
            "params": [],
        },
    ],
    "emitters": [],
    "sessions": [],
}


# -- function handlers ------------------------------------------------------
#
# Every handler hops to a worker thread: they spawn processes, wait on
# subprocesses, and make blocking HTTP probes. Any of those on the event loop
# would stall the control WS and the gateway would take this service for dead.


async def _h_status(args: dict) -> dict:
    proc = await asyncio.to_thread(SUPERVISOR.snapshot)
    health = await asyncio.to_thread(SUPERVISOR.health)
    index = await asyncio.to_thread(binary.index_state)
    version = await asyncio.to_thread(SUPERVISOR.version)
    return {
        "process": proc,
        "version": version,
        "health": health,
        "index": index,
        "front": front.status(),
    }


async def _h_restart(args: dict) -> dict:
    await asyncio.to_thread(SUPERVISOR.stop)
    return await asyncio.to_thread(SUPERVISOR.start)


HANDLERS = {
    "status": _h_status,
    "restart": _h_restart,
}


# -- the supervision loop ---------------------------------------------------


async def _health_loop() -> None:
    """Respawn the upstream server whenever it dies. Never exits.

    Deliberately watches the *process*, not ``/api/health``: a slow or wedged
    HTTP probe on a busy box is not evidence of death, and respawning on it
    would turn a hiccup into a restart loop during the very indexing pass that
    makes the server slow. Process liveness is unambiguous; the health probe is
    reported through ``status`` for a human to judge.
    """
    log.info("claude-view: supervision loop started (interval=%ss)",
             binary.HEALTH_INTERVAL_S)
    while True:
        try:
            await asyncio.sleep(binary.HEALTH_INTERVAL_S)
            res = await asyncio.to_thread(SUPERVISOR.reconcile)
            if res.get("action") == "respawned":
                log.warning("claude-view: upstream server respawned")
        except asyncio.CancelledError:
            return
        except Exception:  # noqa: BLE001 — never let the loop die
            log.exception("claude-view: supervision pass failed")


async def _on_start() -> None:
    """Spawn the binary and the front, then hand off to the loop.

    Neither failure is fatal. The service still registers, so ``status`` can
    report *why* it is broken — which is the visibility a hand-managed daemon
    never had — and the loop keeps retrying the binary.
    """
    if not binary.installed():
        log.warning("claude-view: binary missing at %s — run install.sh; "
                    "the service will register and report this via status",
                    binary.install_dir())
    else:
        try:
            snap = await asyncio.to_thread(SUPERVISOR.start)
            log.info("claude-view ready: pid=%s port=%s data=%s",
                     snap.get("pid"), binary.PORT, binary.STATE_DIR)
        except Exception:  # noqa: BLE001
            log.exception("claude-view: initial spawn failed; loop will retry")

    try:
        front.start()
    except Exception:  # noqa: BLE001 — the binary is still useful on loopback
        log.exception("claude-view: mesh front failed to start")

    asyncio.create_task(_health_loop())


# -- boot -------------------------------------------------------------------


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    _install_sigterm_cleanup()
    await ServiceAdapter(
        "claude-view", API_MANIFEST, HANDLERS, on_start=_on_start,
    ).run()


if __name__ == "__main__":
    asyncio.run(main())
