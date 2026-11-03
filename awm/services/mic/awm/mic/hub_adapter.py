"""Hub adapter for the mic service — the remote microphone.

Boots the mic service as a gateway-registered process on the shared
``awm.gatewayclient.ServiceAdapter`` loop (register → ready → serve →
reconnect). The gateway injects only ``AWM_HUB_URL`` / ``AWM_SERVICE_NAME`` /
``AWM_SERVICE_ID`` — there is no token.

The audio rides the hub like every other stream in awm: the page at ``/ui/mic``
opens a ``stream`` session, the gateway byte-relays it to this process, and
``bridge`` pipes the PCM into the ``virtmic`` null-sink. mic used to run its own
off-host HTTPS listener because ``getUserMedia`` needs a secure context;
``httpsfront`` supplies that for every awm page now, so mic mints no certificates
and binds no port.

mic also owns the audio plumbing: PulseAudio and the null-sink (``audio``). A
separate virtmic service held it until the redux folded it in here. It is kept
alive three ways: the PulseAudio config files, a pass in ``on_start``, and the
health loop below. ``bridge.start_stream`` also ensures the sink right before
it spawns ``pacat``.

Run via ``run.sh`` (which the gateway spawns and respawns):
    python -m awm.mic.hub_adapter

Functions:
  - status       (tool ``mic_status``) — sink name, active stream count, last
                 PCM timestamp, and the sink's health.
  - ensure_sink  (tool ``mic_ensure_sink``) — run one reconcile pass now.

Sessions:
  - kind="stream", transport="direct" — browser mic capture. Opened with
    ``{"sampleRate": <native>, "channels": 1, "format": "s16le"}``; binary
    frames are s16le PCM, text frames are JSON control. Back come ``ready`` /
    ``error`` / ``superseded`` / ``pong``.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from awm import usage
from awm.gatewayclient import ServiceAdapter, SessionContext

from awm.mic import audio, bridge

log = logging.getLogger("awm.mic.hub_adapter")

#: How often the health loop reconciles. Short enough that a PulseAudio
#: restart is repaired well before anyone notices a dead capture source,
#: cheap enough to ignore (a handful of `pactl` probes).
HEALTH_INTERVAL_S = float(os.environ.get("MIC_HEALTH_INTERVAL_S", "20"))

API_MANIFEST: dict[str, Any] = {
    "functions": [
        {
            "name": "status",
            "effect": "read",
            "description": (
                "Report the active stream count, last PCM timestamp, and the "
                "audio path's health: PulseAudio reachability, whether the "
                "virtmic sink is loaded, the default capture source, which "
                "durability layers are installed, and the last heal time."
            ),
            "params": [],
        },
        {
            "name": "ensure_sink",
            "effect": "write",
            "description": (
                "Run one reconcile pass now: ensure the PulseAudio config, "
                "daemon, virtmic null-sink and default capture source are all "
                "in place. Idempotent."
            ),
            "params": [],
        },
    ],
    "emitters": [],
    "sessions": [
        {"kind": "stream", "transport": "direct"},
    ],
}


# -- function handlers ------------------------------------------------------
#
# Both shell out to `pactl`, so they hop to a worker thread: a blocking probe
# on the event loop would stall the control WS and every live audio stream.


async def _h_status(args: dict) -> dict:
    st = bridge.status()
    try:
        sink = await asyncio.to_thread(audio.status, bridge.SINK)
    except Exception as e:  # noqa: BLE001 — a broken PulseAudio must not 500 mic_status
        log.warning("sink status unavailable: %s", e)
        st.update(audio=None, audio_path_ok=None, audio_error=str(e))
        return st
    st.update(audio=sink, sink_present=sink.get("sink_present"),
              default_source=sink.get("default_source"),
              audio_path_ok=bool(sink.get("sink_present") and sink.get("default_source_ok")))
    return st


async def _h_ensure_sink(args: dict) -> dict:
    return await asyncio.to_thread(audio.ensure_once, bridge.SINK)


HANDLERS = {
    "status": _h_status,
    "ensure_sink": _h_ensure_sink,
}


# -- sessions ---------------------------------------------------------------


async def _run_stream_session(ctx: SessionContext) -> None:
    await bridge.run_session(ctx)


SESSION_HANDLERS = {"stream": _run_stream_session}


# -- the health loop --------------------------------------------------------


async def _health_loop() -> None:
    """Durability layer 3: reconcile forever. Sleeps first, so the ``on_start``
    pass is not repeated at once."""
    while True:
        try:
            await asyncio.sleep(HEALTH_INTERVAL_S)
            res = await asyncio.to_thread(audio.ensure_once, bridge.SINK)
            if res.get("healed"):
                log.warning("mic: repaired %s", ", ".join(res["repairs"]))
                usage.record_background("sink-repair")
        except asyncio.CancelledError:
            return
        except Exception:  # noqa: BLE001 — never let the loop die
            log.exception("mic: sink health pass failed")


async def _on_start() -> None:
    """Provision once, then hand off to the loop. A failure still registers,
    so ``mic_status`` can say why the audio path is broken."""
    try:
        res = await asyncio.to_thread(audio.ensure_once, bridge.SINK)
        log.info("mic ready: sink=%s default_source=%s repairs=%s",
                 res["sink"], res["default_source"], res["repairs"] or "none")
    except Exception:  # noqa: BLE001
        log.exception("mic: initial sink provisioning failed; the loop will retry")
    asyncio.get_running_loop().create_task(_health_loop())


# -- boot -------------------------------------------------------------------


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    await ServiceAdapter(
        "mic",
        API_MANIFEST,
        HANDLERS,
        session_handlers=SESSION_HANDLERS,
        on_start=_on_start,
    ).run()


if __name__ == "__main__":
    asyncio.run(main())
