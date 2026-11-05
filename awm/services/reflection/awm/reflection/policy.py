"""The one seam between reflection and the trust lane's policy library (`awm.trustpolicy`).

reflection keeps each session's trust level in its own ledger and asks the
library what a level allows. The gateway refuses what a level may never call
(channels.md R125); a steer stays here, because only this ledger knows the
target's level.

A level belongs to the job as well as to the session id it started under: a
cleared or respawned conversation keeps its job but takes a new session id,
and must keep its level.

A session with no recorded level reads as ``trusted`` only when a harness
plugin knows it: a session started by hand, or before levels existed, acted
with full trust. A session id that neither this ledger nor any harness knows
reads as ``untrusted`` (fleet.md R103 item 2). A caller that is no session, a
user's shell or a service, starts sessions as ``trusted``.

A session is described as the harness describes it: a dict with
``session_id``, ``job`` when it has one, and ``known: False`` when no harness
answered for it.
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
UNKNOWN_SESSION_LEVEL = "untrusted"
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


def level_of(session_id: Optional[str], *, known: bool = False,
             job: Optional[str] = None) -> str:
    """The level of ``session_id``, or of ``job`` under a new session id.

    ``known`` says a harness already answered for it.
    """
    if not session_id:
        return DEFAULT_LEVEL
    level = recorded_level(session_id, job)
    if level:
        return level
    return DEFAULT_LEVEL if known or _harness_knows(session_id) else UNKNOWN_SESSION_LEVEL


def level(who: dict) -> str:
    """The level of a session as a harness described it."""
    return level_of(who.get("session_id"), known=who.get("known", True), job=who.get("job"))


def recorded_level(session_id: Optional[str], job: Optional[str] = None) -> Optional[str]:
    """The level ``record`` wrote for ``session_id``, or for its job; None for no record."""
    if not session_id:
        return None
    data = _read()
    rec = data.get(session_id)
    if isinstance(rec, dict) and rec.get("level"):
        return rec["level"]
    if not job:
        return None
    return next((r["level"] for sid, r in data.items()
                 if isinstance(r, dict) and r.get("level") and r.get("job") == job
                 and sid[:2] == session_id[:2]), None)


def same_session(a: dict, b: dict) -> bool:
    """Whether two descriptions name one session, across a change of session id."""
    sa, sb = a.get("session_id") or "", b.get("session_id") or ""
    if sa and sa == sb:
        return True
    return bool(a.get("job")) and a.get("job") == b.get("job") and sa[:2] == sb[:2]


def _harness_knows(session_id: str) -> bool:
    from awm.reflection import harness
    try:
        harness.for_session(session_id)
    except harness.HarnessError:
        return False
    return True


def record(session_id: str, level: str, origin: str, job: Optional[str] = None) -> None:
    with _lock:
        data = _read()
        data[session_id] = {**(data.get(session_id) or {}), "level": level,
                            "origin": origin, "at_ms": int(time.time() * 1000)}
        if job:
            data[session_id]["job"] = job
        try:
            _write(data)
        except OSError as exc:
            log.warning("reflection: could not record the trust of %s: %s",
                        session_id, exc)


def check_steer(actor: Optional[dict], verb: str, target: Optional[dict] = None) -> None:
    """Raise :class:`Refused` when the caller may not run ``verb`` on another session.

    ``actor`` is None for a caller that is no session. A session acting on
    itself steers nobody.
    """
    if not actor:
        return
    if target and same_session(actor, target):
        return
    ok, why = _lib().may_steer(
        level(actor), verb,
        target_level=level(target) if target and target.get("session_id") else None)
    if not ok:
        raise Refused(why)


def start_level(actor: Optional[dict], requested: Optional[str], *,
                session_id: Optional[str] = None, grant: Any = None) -> Optional[str]:
    """The level a new session starts at. Raises :class:`Refused` above the caller's."""
    lib = _lib()
    starter = level(actor) if actor else DEFAULT_LEVEL
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
