"""`reflection wait`: a session waits until the sessions it lists have retired, then is woken (`sessions.md` R51).

A wait is a record on the waiter's node. A retire is a `reflection stop`: that
node notes the session as retired and tells every domestic peer, and the node
that holds a wait wakes its waiter once every listed session has retired.
Nothing polls. A failed wake raises an alarm and keeps the wait. At start,
:func:`check` reads the waits once and raises an alarm for each listed session
that is neither live nor retired anywhere.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Callable, Iterable, Optional

from awm.reflection import alarm

log = logging.getLogger("awm.reflection.waits")

#: How many retired session ids a node remembers, so a wait set up after its
#: sessions already retired still sees them.
RETIRED_KEPT = 5000

_lock = threading.Lock()

Wake = Callable[[dict], dict]


def _waits_path() -> Path:
    return alarm.state_dir() / "waits.json"


def _retired_path() -> Path:
    return alarm.state_dir() / "retired.json"


def _read(path: Path, empty):
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return empty
    return data if isinstance(data, type(empty)) else empty


def _write(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1))
    os.replace(tmp, path)


def load() -> list[dict]:
    return [w for w in _read(_waits_path(), []) if isinstance(w, dict)]


def _save(rows: list[dict]) -> None:
    _write(_waits_path(), rows)


def retired_among(session_ids: Iterable[str]) -> list[str]:
    """The ids in ``session_ids`` that retired on this node."""
    known = _read(_retired_path(), {})
    return [s for s in session_ids if s in known]


def _note(session_id: str) -> None:
    known = _read(_retired_path(), {})
    known[session_id] = int(time.time() * 1000)
    if len(known) > RETIRED_KEPT:
        known = dict(sorted(known.items(), key=lambda kv: kv[1])[-RETIRED_KEPT:])
    _write(_retired_path(), known)


def add(*, session_id: str, until: list[str], tool: str, job: Optional[str],
        name: str = "", cwd: Optional[str] = None,
        retired: Iterable[str] = ()) -> dict:
    """Record that ``session_id`` waits on ``until``. A second wait replaces the first.

    A waiter with no ``job`` is a terminal session: it is woken by a line typed
    into it, not by a resume.
    """
    where = alarm.scope_of(cwd)
    row = {"session_id": session_id, "until": list(until),
           "retired": [s for s in until if s in set(retired)],
           "tool": tool, "job": job, "name": name,
           "since_ms": int(time.time() * 1000),
           "project": where[0] if where else None,
           "scope": where[1] if where else None}
    with _lock:
        _save([w for w in load() if w["session_id"] != session_id] + [row])
    return row


def drop(session_id: str) -> None:
    with _lock:
        _save([w for w in load() if w["session_id"] != session_id])


def drop_job(job: str) -> None:
    """Clear the wait of the session whose job is ``job``: it was resumed by hand."""
    with _lock:
        _save([w for w in load() if not job or w.get("job") != job])


def done(row: dict) -> bool:
    return set(row["until"]) <= set(row.get("retired") or [])


def wake_text(row: dict) -> str:
    return ("[awm reflection] The sessions you waited on have retired: "
            f"{', '.join(row['until'])}. Read their journals, then carry on. "
            "Automated message from the reflection service; it is not a new "
            "instruction from the user.")


def _wake(row: dict, wake: Wake) -> bool:
    """Wake the waiter. On a failure, raise an alarm and keep the wait."""
    try:
        out = wake(row)
    except Exception as exc:  # noqa: BLE001 - every failure is a failed wake
        out = {"ok": False, "reason": f"{type(exc).__name__}: {exc}"}
    if out.get("ok"):
        drop(row["session_id"])
        return True
    reason = out.get("reason") or out.get("error") or "no reason given"
    log.warning("reflection: could not wake waiter %s: %s", row["session_id"], reason)
    alarm.raise_alarm({
        "session_id": row["session_id"], "name": row.get("name"), "command": "wait",
        "followup": wake_text(row),
        "cwd": (f"/projects/{row['project']}/{row['scope']}"
                if row.get("project") else None),
        "reason": (f"every session it waited on retired, but the wake failed: "
                   f"{reason}. The wait is kept; resume the session by hand")})
    return False


def retired(session_id: str, *, wake: Wake, local: bool = True) -> list[str]:
    """``session_id`` retired. Returns the waiters woken.

    A ``local`` retire is remembered here, so a later wait on this node sees it.
    A retiring waiter's own wait clears. Each wait that lists the session counts
    it, and a wait whose listed sessions have all retired wakes its waiter.
    """
    with _lock:
        if local:
            _note(session_id)
        rows = [w for w in load() if w["session_id"] != session_id]
        ready = []
        for w in rows:
            if session_id in w["until"] and session_id not in (w.get("retired") or []):
                w["retired"] = [*(w.get("retired") or []), session_id]
                if done(w):
                    ready.append(dict(w))
        _save(rows)
    return [w["session_id"] for w in ready if _wake(w, wake)]


def check(*, state: Callable[[str], Optional[str]],
          retired_elsewhere: Callable[[list[str]], list[str]],
          wake: Wake) -> list[str]:
    """Read the waits once at start. Returns the listed sessions found lost.

    ``state(session_id)`` answers ``"live"`` or ``"stopped"`` for a session some
    node knows, else ``"gone"``, or None when no node could say. A listed session
    that retired while this node was down counts as retired now.
    """
    lost = []
    for row in load():
        pending = [s for s in row["until"] if s not in (row.get("retired") or [])]
        for sid in retired_among(pending) + retired_elsewhere(pending):
            retired(sid, wake=wake, local=False)
        row = next((w for w in load() if w["session_id"] == row["session_id"]), None)
        if row is None:
            continue
        for sid in [s for s in row["until"] if s not in (row.get("retired") or [])]:
            if state(sid) != "gone":
                continue
            lost.append(sid)
            alarm.raise_alarm({
                "session_id": row["session_id"], "name": row.get("name"),
                "command": "wait", "followup": wake_text(row),
                "cwd": (f"/projects/{row['project']}/{row['scope']}"
                        if row.get("project") else None),
                "reason": (f"it waits on {sid}, which is neither live nor retired on "
                           f"any node. Recover it by hand: resume {sid}, or wake the "
                           f"waiter and drop the wait")})
    return lost
