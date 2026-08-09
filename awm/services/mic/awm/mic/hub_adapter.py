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
and binds no port. That is also why it can start on a node that holds only the
public half of the fleet CA — which it could not before.

**mic does not own the audio plumbing.** PulseAudio and the ``virtmic``
null-sink belong to the ``virtmic`` service; mic is a consumer that pipes PCM
into the sink with ``pacat``. It used to provision the sink itself at startup —
once, never re-checked — which is why a PulseAudio idle-exit destroyed the sink
permanently while mic went on reporting ``ready`` and recording silence. Two
things follow from the split:

- ``mic_status`` reports the sink's **real** health by asking ``virtmic``,
  rather than a bare ``ready`` that says nothing about the audio path.
- The dependency is explicit rather than temporal. The gateway guarantees no
  start order between services, so mic may well boot before virtmic; instead of
  provisioning at startup, ``bridge.start_stream`` calls ``virtmic ensure``
  immediately before spawning ``pacat``.

Run via ``run.sh`` (which the gateway spawns and respawns):
    python -m awm.mic.hub_adapter

Functions:
  - status       (tool ``mic_status``) — sink name, active stream count, last
                 PCM timestamp, plus the virtmic sink's health as reported by
                 the virtmic service.
  - ensure_sink  (tool ``mic_ensure_sink``) — **deprecated**; forwards to
                 ``virtmic_ensure``. Kept for one release so existing scripts
                 and muscle memory don't break.

Sessions:
  - kind="stream", transport="direct" — browser mic capture. Opened with
    ``{"sampleRate": <native>, "channels": 1, "format": "s16le"}``; binary
    frames are s16le PCM, text frames are JSON control. Back come ``ready`` /
    ``error`` / ``superseded`` / ``pong``.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from awm import gatewayclient
from awm.gatewayclient import ServiceAdapter, SessionContext

from awm.mic import bridge

log = logging.getLogger("awm.mic.hub_adapter")


API_MANIFEST: dict[str, Any] = {
    "functions": [
        {
            "name": "status",
            "description": (
                "Report the active stream count, last PCM timestamp, and the "
                "virtmic sink's health (sink presence and default capture "
                "source, from the virtmic service)."
            ),
            "params": [],
        },
        {
            "name": "ensure_sink",
            "description": (
                "DEPRECATED — use virtmic_ensure. Forwards to the virtmic "
                "service, which owns PulseAudio and the null-sink."
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
# Async, because both of them talk to virtmic over the gateway and the audio
# now shares this process's single event loop. The blocking `call_sync` these
# used to make was correct only while every stream had its own thread.


async def _h_status(args: dict) -> dict:
    """Bridge state plus the audio path's real health.

    The old implementation reported a bare listener status, so mic looked
    perfectly healthy while the sink underneath it was gone. Surfacing
    virtmic's view is what makes that failure visible.
    """
    st = bridge.status()
    try:
        vm = await gatewayclient.call("virtmic", "status", timeout=10.0)
        # A None result comes back as {}, so it can't be used as a not-found
        # signal — key off a field virtmic always sets instead.
        if vm and "sink_present" in vm:
            st["virtmic"] = vm
            st["sink_present"] = vm.get("sink_present")
            st["default_source"] = vm.get("default_source")
            st["audio_path_ok"] = bool(
                vm.get("sink_present") and vm.get("default_source_ok"))
        else:
            st["virtmic"] = None
            st["audio_path_ok"] = None
            st["virtmic_error"] = "virtmic returned no status"
    except Exception as e:  # noqa: BLE001 — virtmic down must not 500 mic_status
        log.warning("virtmic status unavailable: %s", e)
        st["virtmic"] = None
        st["audio_path_ok"] = None
        st["virtmic_error"] = str(e)
    return st


async def _h_ensure_sink(args: dict) -> dict:
    """Deprecated alias — virtmic owns provisioning now."""
    log.info("mic_ensure_sink is deprecated; forwarding to virtmic_ensure")
    res = await gatewayclient.call("virtmic", "ensure", timeout=30.0)
    return {"deprecated": "use virtmic_ensure", **(res or {})}


HANDLERS = {
    "status": _h_status,
    "ensure_sink": _h_ensure_sink,
}


# -- sessions ---------------------------------------------------------------


async def _run_stream_session(ctx: SessionContext) -> None:
    await bridge.run_session(ctx)


SESSION_HANDLERS = {"stream": _run_stream_session}


# -- boot -------------------------------------------------------------------
#
# No `on_start`: there is nothing to provision. That is the ideal end state
# under the ready-ASAP contract, and it is what makes mic startable on a node
# that cannot sign certificates.


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
    ).run()


if __name__ == "__main__":
    asyncio.run(main())
