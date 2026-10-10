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

CAUTION: unreadable means live. A sweep that cannot tell must not delete. The
read is `awm.claudedaemon.roster.live_session_ids`, which raises
`roster.Unreadable` for a record that exists and cannot be read; that error
propagates out of `session_ids`, so the sweep stops instead of acting on a
shorter list than the truth.
"""

from __future__ import annotations

import os
from pathlib import Path

from awm.claudedaemon import roster

SESSIONS = Path(os.path.expanduser("~/.claude/sessions"))
ROSTER = Path(os.path.expanduser("~/.claude/daemon/roster.json"))
JOBS = Path(os.path.expanduser("~/.claude/jobs"))


def session_ids() -> set[str]:
    return roster.live_session_ids(
        sessions_dir=SESSIONS, roster_path=ROSTER, jobs_dir=JOBS)
