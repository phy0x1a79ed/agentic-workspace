"""The one seam between reflection and the trust lane's policy library (`awm.trustpolicy`).

reflection keeps each session's trust level in its own ledger and asks the
library what a level allows. The gateway refuses what a level may never call
(channels.md R125); a steer stays here, because only this ledger knows the
target's level.

A session with no recorded level reads as ``trusted``: every session started
before levels existed acted with full trust, and a level that appears from
nowhere would refuse them.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Optional

from awm.reflection import alarm

log = logging.getLogger("awm.reflection.policy")

DEFAULT_LEVEL = "trusted"
_lock = threading.Lock()


class Refused(PermissionError):
    """The caller's trust level does not allow the verb."""


def _lib():
    import awm.trustpolicy as lib
    return lib


def _path() -> Path:
    return alarm.state_dir() / "trust.json"


def _read() -> dict:
    try:
        data = json.loads(_path().read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write(data: dict) -> None:
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1))
    os.replace(tmp, path)


def level_of(session_id: Optional[str]) -> str:
    return recorded_levels().get(session_id or "") or DEFAULT_LEVEL


def recorded_levels() -> dict[str, str]:
    """Each session's recorded level. A session missing here reads as ``DEFAULT_LEVEL``."""
    return {sid: rec["level"] for sid, rec in _read().items()
            if isinstance(rec, dict) and rec.get("level")}


def record(session_id: str, level: str, origin: str) -> None:
    with _lock:
        data = _read()
        data[session_id] = {**(data.get(session_id) or {}), "level": level,
                            "origin": origin, "at_ms": int(time.time() * 1000)}
        try:
            _write(data)
        except OSError as exc:
            log.warning("reflection: could not record the trust of %s: %s",
                        session_id, exc)


def check_steer(actor_id: Optional[str], verb: str,
                target_id: Optional[str] = None) -> None:
    """Raise :class:`Refused` when the caller may not run ``verb`` on another session."""
    if not actor_id:
        return
    ok, why = _lib().may_steer(level_of(actor_id), verb,
                            target_level=level_of(target_id) if target_id else None)
    if not ok:
        raise Refused(why)


def start_level(actor_id: Optional[str], requested: Optional[str], *,
                session_id: Optional[str] = None, grant: Any = None) -> Optional[str]:
    """The level a new session starts at. Raises :class:`Refused` above the caller's."""
    lib = _lib()
    starter = level_of(actor_id)
    try:
        key = lib.grant_key() if grant else None
    except Exception:  # noqa: BLE001 - no key reads as no grant
        key = None
    try:
        return lib.level_for_start(starter, requested, session_id=session_id,
                                   grant=grant, grant_key=key)
    except (PermissionError, ValueError) as exc:
        raise Refused(str(exc)) from exc


def first_message_hint(actor_id: Optional[str]) -> Optional[str]:
    """The hint for a session's first awm message to a local session, once."""
    if not actor_id:
        return None
    with _lock:
        data = _read()
        rec = data.get(actor_id) or {}
        if rec.get("hinted"):
            return None
        data[actor_id] = {**rec, "hinted": True}
        try:
            _write(data)
        except OSError:
            return None
    return _lib().FIRST_LOCAL_MESSAGE_HINT
