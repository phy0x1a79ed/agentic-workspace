"""TTS preset storage — named engine configs + a last-used pointer.

Keys live in the central awm.config k/v table (``.awm/state.db``):

* ``tts_preset:<name>`` → JSON ``{engine, params}``
* ``tts_last_used``     → JSON ``{engine, params}``

The k/v split mirrors ``awm.services.voice_engine_config``. Built-in
presets are flagged at read-time from an in-module set so seeding can
re-run idempotently and deletion can be refused without a schema change.
"""

from __future__ import annotations

import json
from typing import Any

from awm.persistence.databases import get_connection
from awm.persistence.config_service import get_config, set_config

_KEY_PREFIX = "tts_preset:"
_LAST_USED_KEY = "tts_last_used"

# Names that ``seed_builtin_presets`` writes / refuses to delete. The
# preset content can change at startup; the *name* is what marks it
# built-in to the read path.
_BUILTIN_NAMES: set[str] = {"glados"}


class BuiltinConflict(Exception):
    """Raised when a write/delete targets a built-in preset name."""


def _decode(raw: str | None) -> dict[str, Any] | None:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def list_presets() -> dict[str, dict[str, Any]]:
    """Return ``{name: {engine, params, builtin}}`` across all stored presets."""
    conn = get_connection("tts")
    try:
        rows = conn.execute(
            "SELECT key, value FROM config WHERE key LIKE ? ORDER BY key",
            (f"{_KEY_PREFIX}%",),
        ).fetchall()
    finally:
        conn.close()
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        name = row["key"][len(_KEY_PREFIX):]
        body = _decode(row["value"])
        if not body:
            continue
        out[name] = {
            "engine": body.get("engine"),
            "params": body.get("params") or {},
            "builtin": name in _BUILTIN_NAMES,
        }
    return out


def get_preset(name: str) -> dict[str, Any] | None:
    body = _decode(get_config(_KEY_PREFIX + name))
    if not body:
        return None
    return {
        "engine": body.get("engine"),
        "params": body.get("params") or {},
        "builtin": name in _BUILTIN_NAMES,
    }


def save_preset(name: str, engine: str, params: dict[str, Any]) -> None:
    if name in _BUILTIN_NAMES:
        raise BuiltinConflict(name)
    set_config(_KEY_PREFIX + name, json.dumps({"engine": engine, "params": params or {}}))


def delete_preset(name: str) -> None:
    if name in _BUILTIN_NAMES:
        raise BuiltinConflict(name)
    conn = get_connection("tts")
    try:
        conn.execute("DELETE FROM config WHERE key = ?", (_KEY_PREFIX + name,))
        conn.commit()
    finally:
        conn.close()


def get_last_used() -> dict[str, Any] | None:
    body = _decode(get_config(_LAST_USED_KEY))
    if not body or not body.get("engine"):
        return None
    return {"engine": body["engine"], "params": body.get("params") or {}}


def set_last_used(engine: str, params: dict[str, Any]) -> None:
    set_config(_LAST_USED_KEY, json.dumps({"engine": engine, "params": params or {}}))


# Built-in seed values. Bypass save_preset() because that path refuses
# built-in names — by design — so external callers can't overwrite a
# seed by name.
_BUILTIN_SEEDS: dict[str, dict[str, Any]] = {
    "glados": {
        "engine": "sbv2",
        "params": {
            "sdp_ratio": 0.2,
            "length_scale": 1.1,
            "noise": 0.4,
            "noise_w": 0.8,
            "style": "Standard",
            "style_weight": 1.0,
            "language": "en",
        },
    },
}


def seed_builtin_presets() -> None:
    """Write any missing built-in presets into the config table.

    Idempotent: only seeds names that have no existing row, so an
    operator's manual override (via direct DB edit) survives restart.
    """
    for name, body in _BUILTIN_SEEDS.items():
        if get_config(_KEY_PREFIX + name) is None:
            set_config(_KEY_PREFIX + name, json.dumps(body))
