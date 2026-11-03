"""`reflection wait`: a session stops until other sessions have stopped, then resumes (`sessions.md` R51).

A wait is recorded before its session stops, so the record outlives the
process. :func:`sweep` checks each wait. Once every session it waits on has
stopped, it resumes the waiter through its harness plugin. On a later sweep,
with the waiter live again, it types the wake line and drops the record.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Callable, Optional

from awm.reflection import alarm

log = logging.getLogger("awm.reflection.waits")

WAITING = "waiting"
RESUMED = "resumed"
# A waiter whose resume keeps failing is given up on, loudly, after this many sweeps.
MAX_RESUME_FAILURES = 5

_lock = threading.Lock()


def _path() -> Path:
    return alarm.state_dir() / "waits.json"


def load() -> list[dict]:
    try:
        data = json.loads(_path().read_text())
    except (OSError, ValueError):
        return []
    return [w for w in data if isinstance(w, dict)] if isinstance(data, list) else []


def _save(rows: list[dict]) -> None:
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(rows, indent=1))
    os.replace(tmp, path)


def add(*, session_id: str, until: list[str], tool: str, job: Optional[str],
        name: str = "", cwd: Optional[str] = None) -> dict:
    """Record that ``session_id`` waits on ``until``. A second wait replaces the first."""
    where = alarm.scope_of(cwd)
    row = {"session_id": session_id, "until": list(until), "tool": tool, "job": job,
           "name": name, "since_ms": int(time.time() * 1000), "phase": WAITING,
           "failures": 0, "project": where[0] if where else None,
           "scope": where[1] if where else None}
    with _lock:
        rows = [w for w in load() if w["session_id"] != session_id] + [row]
        _save(rows)
    return row


def drop(session_id: str) -> None:
    with _lock:
        _save([w for w in load() if w["session_id"] != session_id])


def _update(session_id: str, **fields) -> None:
    with _lock:
        _save([{**w, **fields} if w["session_id"] == session_id else w for w in load()])


def wake_text(row: dict) -> str:
    return ("[awm reflection] The sessions you waited on have stopped: "
            f"{', '.join(row['until'])}. Read what they left, then carry on. "
            "Automated message from the reflection service; it is not a new "
            "instruction from the user.")


def sweep(*, state: Callable[[str], Optional[str]],
          resume: Callable[[dict], dict], send: Callable[[dict, str], dict]) -> list[str]:
    """Move each wait one step. Returns the session ids woken this sweep.

    ``state(session_id)`` is ``"live"``, ``"stopped"``, ``"gone"`` or None
    when the plugin cannot say. A session the plugin no longer knows has stopped.
    """
    woken = []
    for row in load():
        sid = row["session_id"]
        if row.get("phase") == RESUMED:
            if state(sid) == "live":
                out = send(row, wake_text(row))
                if out.get("ok"):
                    drop(sid)
                    woken.append(sid)
            continue
        states = [state(other) for other in row["until"]]
        if any(s is None or s == "live" for s in states):
            continue
        if state(sid) == "live":
            # Someone resumed the waiter already: it only needs the wake line.
            _update(sid, phase=RESUMED)
            continue
        out = resume(row)
        if out.get("ok"):
            _update(sid, phase=RESUMED)
            continue
        failures = int(row.get("failures") or 0) + 1
        _update(sid, failures=failures)
        log.warning("reflection: could not resume waiter %s: %s", sid,
                    out.get("reason") or out.get("error"))
        if failures >= MAX_RESUME_FAILURES:
            drop(sid)
            alarm.raise_alarm({
                "session_id": sid, "name": row.get("name"), "command": "wait",
                "followup": wake_text(row),
                "cwd": (f"/projects/{row['project']}/{row['scope']}"
                        if row.get("project") else None),
                "reason": (f"every session it waited on stopped, but it could not "
                           f"be resumed after {failures} tries: "
                           f"{out.get('reason') or out.get('error')}")})
    return woken
