"""TTS engine-exposure helpers.

This module used to host a standalone FastAPI app (``build_app``) with the
``/engines`` / ``/call`` / ``/presets`` / ``/state`` HTTP routes. Under the
modular service contract the gateway talks to the service over the control WS,
not HTTP — ``hub_adapter`` exposes those as ``ServiceAdapter`` functions + the
direct ``call`` playback session. What remains here is the shared logic those
handlers reuse:

* ``EXPOSED_TTS_ENGINES`` — which registry engines the UI surfaces.
* ``_enrich_piper_enums`` — fold the installed local piper voices and the RVC
  sidecar's voice list into piper's JSON-Schema enums so the config form offers
  a piper-voice dropdown and an RVC-voice dropdown (``off`` + sidecar voices).
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

import httpx

log = logging.getLogger("awm.tts.app")

# Engines we surface in the UI. The registry holds more (kokoro_rvc, f5tts,
# gptsovits); they stay loadable from code, but the UI exposes exactly two
# user-facing engines. RVC is *not* a peer engine — it folds into piper as an
# optional voice-swap post-stage (the ``rvc_voice`` dropdown), so kokoro_rvc is
# no longer surfaced standalone.
EXPOSED_TTS_ENGINES = ("piper", "sbv2")


_voices_cache: dict[str, Any] = {"at": 0.0, "data": None}
_VOICES_TTL_S = 30.0


async def _fetch_sidecar_voices() -> dict[str, Any] | None:
    """Pull {tts:[...], rvc:[{label,...}]} from the kokoro_rvc sidecar.

    Cached briefly so listEngines stays cheap on repeated UI mounts. Errors
    are swallowed — schema renders without enums and the user can still
    type a value freely.
    """
    now = time.monotonic()
    if _voices_cache["data"] is not None and now - _voices_cache["at"] < _VOICES_TTL_S:
        return _voices_cache["data"]
    # Sidecar URL + TLS verify are infra knobs read from env (mirrors
    # voice.engines.tts.kokoro_rvc) so they don't leak into the user-
    # facing config schema.
    url = os.environ.get("TTS_RVC_URL", "https://127.0.0.1:12123")
    verify_raw = os.environ.get("TTS_RVC_VERIFY_SSL", "").strip().lower()
    verify = verify_raw in {"1", "true", "yes", "on"}
    try:
        async with httpx.AsyncClient(verify=verify, timeout=2.0) as cli:
            r = await cli.get(f"{url}/voices")
            r.raise_for_status()
            data = r.json()
    except Exception as exc:
        log.info("sidecar /voices unreachable, schema enums skipped: %s", exc)
        return None
    _voices_cache["at"] = now
    _voices_cache["data"] = data
    return data


async def _enrich_piper_enums(entry: dict[str, Any]) -> None:
    """Fill piper's two dropdowns at listEngines time.

    * ``base_voice`` — installed local piper ``.onnx`` models (dir scan). The
      enum is *always* stamped (even when empty): an empty list renders as a
      disabled 'none' dropdown, so the field is structurally a chooser, never a
      free-text box.
    * ``rvc_voice`` — ``off`` plus the RVC sidecar's voice labels. ``off`` is
      always offered (and the default), so the dropdown is useful even when the
      sidecar is unreachable.
    """
    from awm.tts.voice.engines.tts.piper import list_piper_voices

    props = entry.get("schema", {}).get("properties") or {}

    voices = list_piper_voices()
    if "base_voice" in props:
        # `base_voice` is Optional[str] → anyOf [str, null]; stamping enum on
        # the outer field is what DynamicConfigForm's fieldEnum() reads. Always
        # stamp (possibly []) so the enum key is a stable "this is a chooser"
        # signal with its contents as data.
        props["base_voice"]["enum"] = list(voices)

    rvc_options = ["off"]
    sidecar = await _fetch_sidecar_voices()
    if sidecar:
        rvc_options += [v.get("label") for v in (sidecar.get("rvc") or []) if v.get("label")]
    if "rvc_voice" in props:
        props["rvc_voice"]["enum"] = rvc_options
