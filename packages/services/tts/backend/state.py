"""TTS stripe-local UI state — a generic JSON k/v bucket.

Backed by the service-local JSON store (``_store``), under the
``ui_state`` section. Each key holds a single JSON value of arbitrary
shape; consumers store on the way in and read straight back out.

Today's sole customer is the composer's volume slider
(``key="volume"``); the surface is intentionally generic so future
sticky UI knobs drop in without backend churn.
"""

from __future__ import annotations

from typing import Any

import _store

_SECTION = "ui_state"


def list_state() -> dict[str, Any]:
    """Return every stripe-state key/value pair."""
    return dict(_store.get_section(_SECTION))


def get_state(key: str) -> Any | None:
    return _store.get_section(_SECTION).get(key)


def set_state(key: str, value: Any) -> None:
    data = _store.get_section(_SECTION)
    data[key] = value
    _store.set_section(_SECTION, data)


def delete_state(key: str) -> None:
    data = _store.get_section(_SECTION)
    if key in data:
        del data[key]
        _store.set_section(_SECTION, data)
