"""TTS stripe-local UI state — a generic JSON k/v bucket.

Same storage mechanism as ``presets`` (the central ``.awm/state.db``
``config`` table via ``awm.services.config_service``), but pointed at a
different key prefix and with no built-in / last-used niceties. Each
key holds a single JSON value of arbitrary shape; consumers
JSON-encode on the way in and JSON-decode on the way out.

Today's sole customer is the composer's volume slider
(``key="volume"``); the surface is intentionally generic so future
sticky UI knobs drop in without backend churn.
"""

from __future__ import annotations

import json
from typing import Any

from awm.db import get_connection
from awm.services.config_service import get_config, set_config

_KEY_PREFIX = "tts_state:"


def _decode(raw: str | None) -> Any | None:
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def list_state() -> dict[str, Any]:
    """Return every stripe-state key/value pair, with the prefix stripped."""
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT key, value FROM config WHERE key LIKE ? ORDER BY key",
            (f"{_KEY_PREFIX}%",),
        ).fetchall()
    finally:
        conn.close()
    out: dict[str, Any] = {}
    for row in rows:
        name = row["key"][len(_KEY_PREFIX):]
        value = _decode(row["value"])
        if value is not None:
            out[name] = value
    return out


def get_state(key: str) -> Any | None:
    return _decode(get_config(_KEY_PREFIX + key))


def set_state(key: str, value: Any) -> None:
    set_config(_KEY_PREFIX + key, json.dumps(value))


def delete_state(key: str) -> None:
    conn = get_connection()
    try:
        conn.execute("DELETE FROM config WHERE key = ?", (_KEY_PREFIX + key,))
        conn.commit()
    finally:
        conn.close()
