"""Session ids that belong to a Claude Code process running right now.

A session idle for longer than the transcripts retention can still be alive: a
background job parked for a fortnight resumes the moment somebody attaches to it.

Three places name a live session, and all three are read because none is
complete on its own:

* ``~/.claude/sessions/<pid>.json`` — one per running REPL, interactive or
  background. Checked against ``/proc`` because the file outlives the process.
* ``~/.claude/daemon/roster.json`` — the daemon's background workers, which
  carry a ``sessionId`` the job was dispatched with.
* ``~/.claude/jobs/<short>/state.json`` — the job's own record, whose
  ``resumeSessionId`` is where a cleared or respawned conversation went.

CAUTION: unreadable means live. A record that exists and cannot be read raises
`roster.Unreadable`, and the caller must stop instead of acting on a shorter list
than the truth.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from awm.claudedaemon import roster

log = logging.getLogger("awm.cch.live")

SESSIONS = Path(os.path.expanduser("~/.claude/sessions"))
ROSTER = Path(os.path.expanduser("~/.claude/daemon/roster.json"))
JOBS = Path(os.path.expanduser("~/.claude/jobs"))


def session_ids() -> set[str]:
    try:
        return roster.live_session_ids(
            sessions_dir=SESSIONS, roster_path=ROSTER, jobs_dir=JOBS)
    except roster.Unreadable as exc:
        log.error("cch: live sessions unknown, fix or delete %s: %s", exc.path, exc)
        raise
