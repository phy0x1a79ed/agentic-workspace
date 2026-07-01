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

Run via ``run.sh`` (which the gateway spawns and respawns):
    python -m awm.mic.hub_adapter

Functions (sessionless — no PCM rides the hub):
  - status        (tool ``mic_status``) — sink, default source, listener port,
                  TLS state, active stream count, last PCM timestamp.
  - ensure_sink   (tool ``mic_ensure_sink``) — re-provision the virtmic sink
                  idempotently.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

from awm.gatewayclient import ServiceAdapter

from awm.mic import audio, bridge, certs

log = logging.getLogger("awm.mic.hub_adapter")

HERE = Path(__file__).resolve().parent          # awm/services/mic/awm/mic
SERVICE_DIR = HERE.parents[1]                    # awm/services/mic
STATIC_DIR = SERVICE_DIR / "static"
CERT_DIR = SERVICE_DIR / ".certs"
SANS_FILE = SERVICE_DIR / ".sans"      # host-specific extra SANs (gitignored)

PORT = int(os.environ.get("MIC_PORT", "12200"))
SINK = os.environ.get("MIC_SINK", "virtmic")


API_MANIFEST: dict[str, Any] = {
    "functions": [
        {
            "name": "status",
            "description": (
                "Report the virtmic sink, the PulseAudio default capture "
                "source, the HTTPS listener port, TLS state, and the active "
                "stream count."
            ),
            "params": [],
        },
        {
            "name": "ensure_sink",
            "description": (
                "Idempotently (re)provision the PulseAudio virtmic null-sink "
                "and set it as the default capture source."
            ),
            "params": [],
        },
    ],
    "emitters": [],
    "sessions": [],
}


# -- function handlers ------------------------------------------------------


def _h_status(args: dict) -> dict:
    st = bridge.status()
    st["default_source"] = audio.default_source()
    return st


def _h_ensure_sink(args: dict) -> dict:
    return audio.ensure_sink(SINK)


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
    """Provision audio + certs, then launch the HTTPS bridge in a daemon thread.

    Runs once before the first control-WS connect. Sink provisioning failures
    are non-fatal (recoverable via ``mic_ensure_sink``); a cert failure is fatal
    since the listener can't come up without TLS.
    """
    audio.ensure_runtime_env()
    try:
        audio.ensure_sink(SINK)
    except Exception:  # noqa: BLE001
        log.exception("virtmic provisioning failed; retry with mic_ensure_sink")

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
