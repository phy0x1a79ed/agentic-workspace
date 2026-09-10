"""Session ids that belong to a Claude Code process running right now.

A session idle for longer than the retention can still be alive: a background
job parked for a fortnight resumes the moment somebody attaches to it. Archiving
its transcript out from under it would leave the process writing to a path that
no longer exists, and the sweep this service replaces had exactly that hazard.

Three places name a live session, and all three are read because none is
complete on its own:

* ``~/.claude/sessions/<pid>.json`` — one per running REPL, interactive or
  background. Checked against ``/proc`` because the file outlives the process.
* ``~/.claude/daemon/roster.json`` — the daemon's background workers, which
  carry a ``sessionId`` the job was dispatched with.
* ``~/.claude/jobs/<short>/state.json`` — the job's own record, whose
  ``resumeSessionId`` is where a cleared or respawned conversation went. The
  roster keeps the id the job started with, so this is the only place the
  current one appears.

CAUTION: unreadable means live. A sweep that cannot tell must not delete.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

SESSIONS = Path(os.path.expanduser("~/.claude/sessions"))
ROSTER = Path(os.path.expanduser("~/.claude/daemon/roster.json"))
JOBS = Path(os.path.expanduser("~/.claude/jobs"))


def _running(pid: int) -> bool:
    return Path(f"/proc/{pid}").exists()


def _load(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def session_ids() -> set[str]:
    ids: set[str] = set()

    try:
        records = list(SESSIONS.glob("*.json"))
    except OSError:
        records = []
    for rec in records:
        try:
            pid = int(rec.stem)
        except ValueError:
            continue
        if not _running(pid):
            continue
        sid = _load(rec).get("sessionId")
        if sid:
            ids.add(str(sid))

    for worker in (_load(ROSTER).get("workers") or {}).values():
        sid = worker.get("sessionId")
        if sid:
            ids.add(str(sid))

    try:
        jobs = [d for d in JOBS.iterdir() if d.is_dir()]
    except OSError:
        jobs = []
    for job in jobs:
        state = _load(job / "state.json")
        for key in ("sessionId", "resumeSessionId"):
            sid = state.get(key)
            if sid:
                ids.add(str(sid))

    return ids
