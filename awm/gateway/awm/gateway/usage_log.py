"""Usage events per plugin (contract § Usage event, R132), one JSONL file per day.

The Rust gateway owns this log after its cutover. Until then the Python
gateway writes the events plugins send it, a ``background`` frame on the
control WS and a ``POST /usage`` body, into the same
``<AWM_DIR>/usage/<YYYY-MM-DD>.jsonl`` files.
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import os
import select
from pathlib import Path
from typing import Any

from awm import config

log = logging.getLogger("awm.gateway.usage_log")

SURFACES = frozenset({"mcp", "cli", "http", "plugin", "page", "code", "background"})
FIELDS = ("plugin", "surface", "tool", "verb", "name", "ok", "ms")
#: One ``write`` of at most this many bytes to an ``O_APPEND`` file lands whole,
#: so two writers never interleave a line.
MAX_LINE = getattr(select, "PIPE_BUF", 4096)


def directory() -> Path:
    return Path(config.AWM_DIR) / "usage"


def event(raw: dict[str, Any]) -> dict[str, Any]:
    """The v1 event for ``raw``, stamped now. Raises ``ValueError`` on a bad one."""
    plugin, surface = raw.get("plugin"), raw.get("surface")
    if not isinstance(plugin, str) or not plugin.strip():
        raise ValueError("usage event needs a plugin")
    if surface not in SURFACES:
        raise ValueError(f"surface must be one of {sorted(SURFACES)}")
    out: dict[str, Any] = {"v": 1, "ts": _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    for key in FIELDS:
        value = raw.get(key)
        if key in ("tool", "verb", "name") and value is not None and not isinstance(value, str):
            raise ValueError(f"{key} must be a string")
        if key == "ok" and value is not None and not isinstance(value, bool):
            raise ValueError("ok must be a boolean")
        if key == "ms" and value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))):
            raise ValueError("ms must be a number")
        out[key] = value
    return out


def append(raw: dict[str, Any]) -> bool:
    """Write one event to today's file. Never raises: a count must not fail its caller."""
    try:
        line = (json.dumps(event(raw), separators=(",", ":")) + "\n").encode()
        if len(line) > MAX_LINE:
            raise ValueError(f"usage event over {MAX_LINE} bytes")
        folder = directory()
        folder.mkdir(parents=True, exist_ok=True)
        fd = os.open(folder / f"{_dt.datetime.now(_dt.timezone.utc):%Y-%m-%d}.jsonl",
                     os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        try:
            os.write(fd, line)
        finally:
            os.close(fd)
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("usage event dropped: %s", exc)
        return False
