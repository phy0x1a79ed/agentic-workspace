"""Hub adapter for the hermes service — Nous Research's Hermes Agent.

Registers on the shared ``ServiceAdapter`` loop (register → ready → serve →
reconnect), so the dashboard is visible in ``awm services list``, its health is
a verb rather than a ``systemctl status``, and it is reachable from any node in
the fleet.

Two things live under this one supervised process:

  - the dashboard, adopted rather than parented so an awm deploy never
    interrupts a live chat session — see ``daemon``,
  - a ``kind=url`` mount at ``/ui/hermes`` putting its GUI behind awm's edge
    session — see ``mount``.

The mount dies with this process, which is right: it is pure plumbing and costs
nothing to rebuild. The dashboard does not, which is also right.

Run via ``run.sh`` (which the gateway spawns and respawns):
    python -m awm.hermes.hub_adapter
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from awm.gatewayclient import ServiceAdapter, spawn_supervised

from awm.hermes import daemon, mount

log = logging.getLogger("awm.hermes.hub_adapter")

SUPERVISOR = daemon.Supervisor()


#: ``hermes`` is already a single token, so the gateway's fold — which splits a
#: projected tool name on its *first* underscore — lands these as domain
#: ``hermes`` / verb ``status`` without the contortion ``claude-science`` needs.
API_MANIFEST: dict[str, Any] = {
    "functions": [
        {
            "name": "status",
            "tool": "hermes_status",
            "description": (
                "Report the Hermes Agent dashboard: whether it is listening and "
                "at what pid/port, whether this service adopted it or started "
                "it, the transient user unit it runs in (null means it will not "
                "survive an awm restart), the gateway mount's state and public "
                "URL, and the configured provider and model."
            ),
            "params": [],
            "timeout": 60,
        },
        {
            "name": "start",
            "tool": "hermes_start",
            "description": (
                "Adopt the running dashboard, or launch one into a transient "
                "user unit if none is serving. Idempotent. The dashboard "
                "deliberately outlives this service, so this is safe to call at "
                "any time."
            ),
            "params": [],
            "timeout": 240,
        },
        {
            "name": "stop",
            "tool": "hermes_stop",
            "description": (
                "Stop the dashboard, ending live chat sessions and PTYs. "
                "Idempotent. Stopping the *service* does not do this — you have "
                "to mean it."
            ),
            "params": [],
            "timeout": 120,
        },
        {
            "name": "restart",
            "tool": "hermes_restart",
            "description": (
                "Stop the dashboard and start it again — e.g. to pick up a "
                "`hermes update` or a config change the running process cached. "
                "Interrupts live sessions."
            ),
            "params": [],
            "timeout": 300,
        },
        {
            "name": "logs",
            "tool": "hermes_logs",
            "description": (
                "Tail the dashboard's output: the transient unit's journal when "
                "this service launched it, hermes' own log files when the "
                "dashboard was adopted. The answering source is reported."
            ),
            "params": [
                {"name": "tail", "type": "number",
                 "description": "Lines to return (default 200)."},
            ],
            "timeout": 60,
        },
        {
            "name": "url",
            "tool": "hermes_url",
            "description": (
                "Where to reach the dashboard: the gateway mount for a browser "
                "already holding an awm session, and the loopback address for "
                "an SSH tunnel."
            ),
            "params": [],
        },
        {
            "name": "model",
            "tool": "hermes_model",
            "description": (
                "Read the configured provider, default model and base URL. Pass "
                "`model` to change the default — it is written to config.yaml, "
                "so restart the dashboard for a running session to see it."
            ),
            "params": [
                {"name": "model", "type": "string",
                 "description": "Model slug to set as the default, e.g. "
                                "'deepseek/deepseek-v4-flash-0731'. Omit to read."},
            ],
            "timeout": 120,
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


def _status() -> dict[str, Any]:
    return {
        "dashboard": SUPERVISOR.snapshot(),
        "health": daemon.health(),
        "mount": {**mount.status(), "public_url": mount.public_url()},
        "model": daemon.model_config(),
    }


async def _h_status(args: dict) -> dict:
    return await asyncio.to_thread(_status)


async def _h_start(args: dict) -> dict:
    return await asyncio.to_thread(SUPERVISOR.start)


async def _h_stop(args: dict) -> dict:
    return await asyncio.to_thread(SUPERVISOR.stop)


async def _h_restart(args: dict) -> dict:
    return await asyncio.to_thread(SUPERVISOR.restart)


async def _h_logs(args: dict) -> dict:
    return await asyncio.to_thread(SUPERVISOR.logs, int(args.get("tail") or 200))


async def _h_url(args: dict) -> dict:
    return {
        "mount": mount.public_url(),
        "loopback": f"http://127.0.0.1:{daemon.PORT}/",
        "mounted": mount.status().get("mounted"),
    }


async def _h_model(args: dict) -> dict:
    model = (args.get("model") or "").strip()
    if not model:
        return await asyncio.to_thread(daemon.model_config)
    return await asyncio.to_thread(daemon.set_model, model)


HANDLERS = {
    "status": _h_status,
    "start": _h_start,
    "stop": _h_stop,
    "restart": _h_restart,
    "logs": _h_logs,
    "url": _h_url,
    "model": _h_model,
}


# -- the supervision loop ---------------------------------------------------


async def _health_loop() -> None:
    """Relaunch the dashboard whenever it has actually died. Never exits."""
    log.info("hermes: supervision loop started (interval=%ss)",
             daemon.HEALTH_INTERVAL_S)
    while True:
        try:
            await asyncio.sleep(daemon.HEALTH_INTERVAL_S)
            res = await asyncio.to_thread(SUPERVISOR.reconcile)
            if res.get("action") == "respawned":
                log.warning("hermes: dashboard respawned")
        except Exception:  # noqa: BLE001 — never let the loop die
            # CancelledError is a BaseException and passes through, which is
            # what the supervisor above wants: a *return* would read as a
            # defect and be respawned, but a cancellation is a shutdown.
            log.exception("hermes: supervision pass failed")


async def _on_start() -> None:
    """Raise the mount and the loop, then adopt-or-start the dashboard.

    The two background tasks go up first because they are instant, and the
    adopt-or-start can take a minute on a cold node — the orphan reaper kills a
    lease-holder that has not become ready, so nothing slow belongs ahead of
    them. No failure here is fatal: the service still registers, so ``status``
    can report *why* it is broken.
    """
    # Both are long-lived and both fail silently under a bare create_task whose
    # handle nobody reads: a mount that died on its first line looks exactly
    # like one nobody has visited. spawn_supervised logs at ERROR and respawns.
    spawn_supervised("hermes:mount", mount.hold)
    spawn_supervised("hermes:health", _health_loop)

    if not daemon.installed():
        log.warning("hermes: launcher missing at %s — run install.sh; the "
                    "service will register and report this via status", daemon.BIN)
        return
    try:
        snap = await asyncio.to_thread(SUPERVISOR.start)
        log.info("hermes: dashboard %s pid=%s port=%s unit=%s",
                 "adopted" if snap.get("adopted") else "started",
                 snap.get("pid"), snap.get("port"),
                 (snap.get("user_unit") or {}).get("unit"))
    except Exception:  # noqa: BLE001
        log.exception("hermes: initial adopt/start failed; the loop will retry")


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    await ServiceAdapter("hermes", API_MANIFEST, HANDLERS, on_start=_on_start).run()


if __name__ == "__main__":
    asyncio.run(main())
