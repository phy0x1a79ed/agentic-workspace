"""Hub adapter for the mic service — the remote microphone bridge.

Boots the mic service as a gateway-registered process on the shared
``awm.gatewayclient.ServiceAdapter`` loop (register → ready → serve →
reconnect). The gateway injects only ``AWM_HUB_URL`` / ``AWM_SERVICE_NAME`` /
``AWM_SERVICE_ID`` — there is no token.

What the gateway registration buys here is **supervision + a status surface**,
NOT the audio transport. The audio + page ride a self-contained off-host HTTPS
listener (``bridge``), launched in a daemon thread from ``on_start``, because a
phone over ZeroTier needs a secure context for ``getUserMedia`` and the
loopback-only hub can't serve it. When the gateway drains/respawns, this process
exits and the listener dies with it — one supervised lifetime.

**mic does not own the audio plumbing.** PulseAudio and the ``virtmic``
null-sink belong to the ``virtmic`` service; mic is a consumer that pipes PCM
into the sink with ``pacat``. It used to provision the sink itself in
``on_start`` — once, never re-checked — which is why a PulseAudio idle-exit
destroyed the sink permanently while mic went on reporting ``ready`` and
recording silence. Two things follow from the split:

- ``mic_status`` reports the sink's **real** health by asking ``virtmic``,
  rather than a bare ``ready`` that says nothing about the audio path.
- The dependency is made explicit rather than temporal. The gateway guarantees
  no start order between services, so mic may well boot before virtmic; instead
  of provisioning at startup, ``bridge._start_stream`` calls ``virtmic_ensure``
  immediately before spawning ``pacat``. The sink is then guaranteed present at
  the moment audio actually needs it, not merely at boot.

Run via ``run.sh`` (which the gateway spawns and respawns):
    python -m awm.mic.hub_adapter

Functions (sessionless — no PCM rides the hub):
  - status       (tool ``mic_status``) — listener port, TLS state, active
                 stream count, last PCM timestamp, plus the virtmic sink's
                 health as reported by the virtmic service.
  - ensure_sink  (tool ``mic_ensure_sink``) — **deprecated**; forwards to
                 ``virtmic_ensure``. Kept for one release so existing scripts
                 and muscle memory don't break.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

from awm import gatewayclient
from awm.gatewayclient import ServiceAdapter

from awm.mic import bridge, certs

log = logging.getLogger("awm.mic.hub_adapter")

HERE = Path(__file__).resolve().parent          # awm/services/mic/awm/mic
SERVICE_DIR = HERE.parents[1]                    # awm/services/mic
STATIC_DIR = SERVICE_DIR / "static"
CERT_DIR = SERVICE_DIR / ".certs"
SANS_FILE = SERVICE_DIR / ".sans"      # host-specific extra SANs (gitignored)

PORT = int(os.environ.get("MIC_PORT", "12200"))

#: Which sink to feed. The *name* still lives here because mic has to hand it
#: to ``pacat``; the responsibility for creating it belongs to virtmic.
SINK = os.environ.get("MIC_SINK", "virtmic")


API_MANIFEST: dict[str, Any] = {
    "functions": [
        {
            "name": "status",
            "description": (
                "Report the HTTPS listener port, TLS state, active stream "
                "count, and the virtmic sink's health (sink presence and "
                "default capture source, from the virtmic service)."
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
    "sessions": [],
}


# -- function handlers ------------------------------------------------------
#
# Sync handlers, so the adapter runs them on a worker thread (asyncio.to_thread)
# — which is what makes the blocking `call_sync` below correct here. Calling it
# from the event loop would deadlock the control WS.


def _h_status(args: dict) -> dict:
    """Bridge state plus the audio path's real health.

    The old implementation reported a bare listener status, so mic looked
    perfectly healthy while the sink underneath it was gone. Surfacing
    virtmic's view is what makes that failure visible.
    """
    st = bridge.status()
    try:
        vm = gatewayclient.call_sync("virtmic", "status", timeout=10.0)
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


def _h_ensure_sink(args: dict) -> dict:
    """Deprecated alias — virtmic owns provisioning now."""
    log.info("mic_ensure_sink is deprecated; forwarding to virtmic_ensure")
    res = gatewayclient.call_sync("virtmic", "ensure", timeout=30.0)
    return {"deprecated": "use virtmic_ensure", **(res or {})}


HANDLERS = {
    "status": _h_status,
    "ensure_sink": _h_ensure_sink,
}


# -- startup orchestration --------------------------------------------------


def _serve_forever(info: dict) -> None:
    """Run the bridge listener, restarting it on crash. Lives in a daemon
    thread for the life of the process (= the life of the gateway lease)."""
    while True:
        try:
            bridge.serve(
                port=PORT,
                cert=info["cert"],
                key=info["key"],
                ca=info["ca"],
                static_dir=str(STATIC_DIR),
                sink=SINK,
            )
        except Exception:  # noqa: BLE001
            log.exception("bridge listener crashed; restarting in 2s")
            time.sleep(2)


def _on_start() -> None:
    """Provision certs, then launch the HTTPS bridge in a daemon thread.

    No audio provisioning happens here any more — virtmic owns that, and
    ``bridge._start_stream`` ensures the sink at the moment a stream actually
    starts. A cert failure is still fatal, since the listener can't come up
    without TLS.
    """
    # Include operator-declared SANs (e.g. the Windows ZeroTier IP the phone
    # connects to) that this host can't auto-enumerate from inside WSL.
    sans = certs.resolve_sans(san_file=SANS_FILE)
    info = certs.ensure_certs(CERT_DIR, sans=sans)
    log.info("certs ready (SAN=%s)", info["san"])

    t = threading.Thread(
        target=_serve_forever, args=(info,), daemon=True, name="mic-bridge"
    )
    t.start()
    log.info("mic bridge thread launched on :%d (tls on)", PORT)


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
        on_start=_on_start,
    ).run()


if __name__ == "__main__":
    asyncio.run(main())
