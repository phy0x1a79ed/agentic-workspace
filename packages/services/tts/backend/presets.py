"""TTS preset storage — named engine configs + a last-used pointer.

Backed by the service-local JSON store (``_store``); see that module for
the file layout. Two sections are used here:

* ``presets``   → ``{name: {engine, params}}``
* ``last_used`` → ``{engine, params}``

Built-in presets are flagged at read-time from an in-module set so
seeding can re-run idempotently and deletion can be refused without a
schema change.
"""

from __future__ import annotations

from typing import Any

import _store

_PRESETS_SECTION = "presets"
_LAST_USED_SECTION = "last_used"

# Names that ``seed_builtin_presets`` writes / refuses to delete. The
# preset content can change at startup; the *name* is what marks it
# built-in to the read path.
_BUILTIN_NAMES: set[str] = {"glados"}


class BuiltinConflict(Exception):
    """Raised when a write/delete targets a built-in preset name."""


def list_presets() -> dict[str, dict[str, Any]]:
    """Return ``{name: {engine, params, builtin}}`` across all stored presets."""
    out: dict[str, dict[str, Any]] = {}
    for name, body in _store.get_section(_PRESETS_SECTION).items():
        if not isinstance(body, dict):
            continue
        out[name] = {
            "engine": body.get("engine"),
            "params": body.get("params") or {},
            "builtin": name in _BUILTIN_NAMES,
        }
    return out


def get_preset(name: str) -> dict[str, Any] | None:
    body = _store.get_section(_PRESETS_SECTION).get(name)
    if not isinstance(body, dict):
        return None
    return {
        "engine": body.get("engine"),
        "params": body.get("params") or {},
        "builtin": name in _BUILTIN_NAMES,
    }


def save_preset(name: str, engine: str, params: dict[str, Any]) -> None:
    if name in _BUILTIN_NAMES:
        raise BuiltinConflict(name)
    presets = _store.get_section(_PRESETS_SECTION)
    presets[name] = {"engine": engine, "params": params or {}}
    _store.set_section(_PRESETS_SECTION, presets)


def delete_preset(name: str) -> None:
    if name in _BUILTIN_NAMES:
        raise BuiltinConflict(name)
    presets = _store.get_section(_PRESETS_SECTION)
    if name in presets:
        del presets[name]
        _store.set_section(_PRESETS_SECTION, presets)


def get_last_used() -> dict[str, Any] | None:
    body = _store.get_section(_LAST_USED_SECTION)
    if not body or not body.get("engine"):
        return None
    return {"engine": body["engine"], "params": body.get("params") or {}}


def set_last_used(engine: str, params: dict[str, Any]) -> None:
    _store.set_section(
        _LAST_USED_SECTION, {"engine": engine, "params": params or {}}
    )


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
    """Write any missing built-in presets into the store.

    Idempotent: only seeds names that have no existing row, so an
    operator's manual override (via direct file edit) survives restart.
    """
    presets = _store.get_section(_PRESETS_SECTION)
    changed = False
    for name, body in _BUILTIN_SEEDS.items():
        if name not in presets:
            presets[name] = body
            changed = True
    if changed:
        _store.set_section(_PRESETS_SECTION, presets)
