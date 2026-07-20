"""Hub adapter for the virtmic service — PulseAudio ownership.

Registers with the gateway on the shared ``awm.gatewayclient.ServiceAdapter``
loop (register → ready → serve → reconnect), so PulseAudio and the ``virtmic``
null-sink get the same lifecycle every other awm backend has: started on boot,
supervised, respawned on crash. Before this service existed the sink was a
side effect of the ``mic`` service's ``on_start`` — provisioned exactly once,
never re-checked, and silently lost on every PulseAudio idle-exit.

Unlike ``mic`` or ``fileviewer`` this service owns **no listener and no
transport at all**. Its entire job is the background reconcile loop: keep
PulseAudio resident, keep the sink loaded, keep it the default capture source.
Consumers (``mic``'s ``pacat`` bridge, ``stt``, ``arecord``, ``/voice``) just
read the default source and never touch ``pactl`` themselves.

Run via ``run.sh`` (which the gateway spawns and respawns):
    python -m awm.virtmic.hub_adapter

Functions:
  - status  (tool ``virtmic_status``) — PulseAudio reachability, whether the
            daemon is systemd-managed, sink presence, current default source,
            which durability layers are in place, and the last heal timestamp.
  - ensure  (tool ``virtmic_ensure``) — run one reconcile pass now.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from awm.gatewayclient import ServiceAdapter

from awm.virtmic import audio

log = logging.getLogger("awm.virtmic.hub_adapter")

SINK = os.environ.get("VIRTMIC_SINK", audio.DEFAULT_SINK)

#: How often the health loop reconciles. Short enough that a PulseAudio
#: restart is repaired well before anyone notices a dead capture source,
#: cheap enough to ignore (a handful of `pactl` probes).
HEALTH_INTERVAL_S = float(os.environ.get("VIRTMIC_HEALTH_INTERVAL_S", "20"))


API_MANIFEST: dict[str, Any] = {
    "functions": [
        {
            "name": "status",
            "description": (
                "Report virtual-microphone health: PulseAudio reachability, "
                "whether the daemon is systemd-managed, whether the virtmic "
                "sink is loaded, the current default capture source, which "
                "durability layers are installed, and the last heal time."
            ),
            "params": [],
        },
        {
            "name": "ensure",
            "description": (
                "Run one reconcile pass now: ensure the PulseAudio config, "
                "daemon, virtmic null-sink, and default capture source are all "
                "in place. Idempotent."
            ),
            "params": [],
        },
    ],
    "emitters": [],
    "sessions": [],
}


# -- function handlers ------------------------------------------------------
#
# Both handlers shell out to `pactl`, so they hop to a worker thread: a
# blocking probe on the event loop would stall the control WS and the gateway
# would take us for dead.


async def _h_status(args: dict) -> dict:
    return await asyncio.to_thread(audio.status, SINK)


async def _h_ensure(args: dict) -> dict:
    return await asyncio.to_thread(audio.ensure_once, SINK)


HANDLERS = {
    "status": _h_status,
    "ensure": _h_ensure,
}


# -- the health loop --------------------------------------------------------


async def _health_loop() -> None:
    """Durability layer 3: reconcile forever.

    Layers 1 and 2 (``daemon.conf`` / ``default.pa``) should make this a no-op
    in steady state — it exists because the failure mode is silent, so
    "shouldn't happen" is not good enough. Sleeps first so the ``on_start``
    pass isn't immediately repeated.
    """
    log.info("virtmic: health loop started (interval=%ss)", HEALTH_INTERVAL_S)
    while True:
        try:
            await asyncio.sleep(HEALTH_INTERVAL_S)
            res = await asyncio.to_thread(audio.ensure_once, SINK)
            if res.get("healed"):
                log.warning("virtmic: repaired %s", ", ".join(res["repairs"]))
        except asyncio.CancelledError:
            return
        except Exception:  # noqa: BLE001 — never let the loop die
            log.exception("virtmic: health pass failed")


async def _on_start() -> None:
    """Provision immediately, then hand off to the loop.

    A provisioning failure here is non-fatal: the service still registers (so
    ``virtmic_status`` can report *why* it's broken, which is exactly the
    visibility that was missing before) and the loop retries every interval.
    """
    try:
        res = await asyncio.to_thread(audio.ensure_once, SINK)
        log.info("virtmic ready: sink=%s default_source=%s repairs=%s",
                 res["sink"], res["default_source"], res["repairs"] or "none")
    except Exception:  # noqa: BLE001
        log.exception("virtmic: initial provisioning failed; loop will retry")

    asyncio.create_task(_health_loop())


# -- boot -------------------------------------------------------------------


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    await ServiceAdapter(
        "virtmic", API_MANIFEST, HANDLERS, on_start=_on_start,
    ).run()


if __name__ == "__main__":
    asyncio.run(main())
