"""Voice engine config — persisted global selection for STT and TTS.

Voice has no per-room engine state: at any moment one STT engine and one
TTS engine are loaded process-wide. Their selection (engine id + params)
lives in the ``config`` k/v table so it survives uvicorn restarts.

The selection is hardware-specific and intentionally NOT replicated — the
``config`` table isn't a cr-sqlite CRR.
"""

from __future__ import annotations

import json
from typing import Any

from awm.services.config_service import get_config, set_config


_KINDS = ("stt", "tts")


def _engine_key(kind: str) -> str:
    return f"voice_{kind}_engine"


def _params_key(kind: str) -> str:
    return f"voice_{kind}_params"


def get_global() -> dict[str, dict[str, Any] | None]:
    """Return ``{kind: {engine_id, params} | None}`` for stt + tts."""
    out: dict[str, dict[str, Any] | None] = {}
    for kind in _KINDS:
        engine_id = get_config(_engine_key(kind))
        if not engine_id:
            out[kind] = None
            continue
        raw = get_config(_params_key(kind)) or "{}"
        try:
            params = json.loads(raw)
        except json.JSONDecodeError:
            params = {}
        out[kind] = {"engine_id": engine_id, "params": params}
    return out


def set_global(kind: str, engine_id: str, params: dict[str, Any]) -> None:
    if kind not in _KINDS:
        raise ValueError(f"unknown voice engine kind {kind!r}; expected one of {_KINDS}")
    set_config(_engine_key(kind), engine_id)
    set_config(_params_key(kind), json.dumps(params or {}))
