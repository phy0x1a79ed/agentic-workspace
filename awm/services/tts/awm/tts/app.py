"""TTS engine-exposure helpers.

This module used to host a standalone FastAPI app (``build_app``) with the
``/engines`` / ``/call`` / ``/presets`` / ``/state`` HTTP routes. Under the
modular service contract the gateway talks to the service over the control WS,
not HTTP — ``hub_adapter`` exposes those as ``ServiceAdapter`` functions + the
direct ``call`` playback session. What remains here is the shared logic those
handlers reuse:

* ``EXPOSED_TTS_ENGINES`` — which registry engines the UI surfaces.
* ``_enrich_kokoro_rvc_enums`` — fold the kokoro_rvc sidecar's live voice list
  into the engine's JSON-Schema enums so the config form offers them.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

import httpx

log = logging.getLogger("awm.tts.app")

# Engines we surface in the UI. The registry holds more (f5tts, gptsovits);
# they stay loadable from code, but the UI only exposes the production-path set.
EXPOSED_TTS_ENGINES = ("kokoro_rvc", "piper", "sbv2")


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


async def _enrich_kokoro_rvc_enums(entry: dict[str, Any]) -> None:
    voices = await _fetch_sidecar_voices()
    if not voices:
        return
    props = entry.get("schema", {}).get("properties") or {}
    tts_list = voices.get("tts") or []
    rvc_list = [v.get("label") for v in (voices.get("rvc") or []) if v.get("label")]
    if tts_list and "tts_voice" in props:
        props["tts_voice"]["enum"] = list(tts_list)
    if rvc_list and "rvc_label" in props:
        # rvc_label is Optional[str] → anyOf [str, null]. Stamping enum on
        # the outer field is what DynamicConfigForm's fieldEnum() reads.
        props["rvc_label"]["enum"] = list(rvc_list)
