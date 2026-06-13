"""Local JSON persistence for the tts service — stop-gap.

Replaces the previous ``awm.db`` / ``awm.services.config_service`` backing
so the tts service owns its own state instead of reaching into awm's
central ``.awm/state.db``. A sibling scope is building a reusable
persistence module that will eventually supersede this; until then all
tts state lives in one JSON file local to this service package.

Layout of the single file (``<pkg>/.state/tts.json``):

    {
      "presets":   { "<name>": {"engine": ..., "params": {...}} },
      "last_used": { "engine": ..., "params": {...} },
      "ui_state":  { "<key>": <any-json> }
    }

The path is anchored to this module's location (not ``AWM_WORKSPACE``), so
each service worktree keeps its own file. Single-process async service →
the read-modify-write below needs no locking; writes are atomic via a
temp-file + ``os.replace`` so a crash mid-write can't truncate the file.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

# state.py / presets.py / _store.py all live in <pkg>/backend/, so the
# package root is one level up. Keep runtime state out of the code dir.
_STATE_FILE = Path(__file__).resolve().parent.parent / ".state" / "tts.json"


def _read_all() -> dict[str, Any]:
    try:
        data = json.loads(_STATE_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_all(data: dict[str, Any]) -> None:
    _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(_STATE_FILE.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2, sort_keys=True)
        os.replace(tmp, _STATE_FILE)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def get_section(section: str) -> dict[str, Any]:
    """Return a top-level section as a dict (``{}`` if absent/malformed)."""
    val = _read_all().get(section)
    return val if isinstance(val, dict) else {}


def set_section(section: str, value: dict[str, Any]) -> None:
    """Replace a whole top-level section and persist."""
    data = _read_all()
    data[section] = value
    _write_all(data)
