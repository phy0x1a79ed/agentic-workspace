"""Typing one line into a background session that is named by its job id.

`reflection` reaches the session that called it and nobody else. A service that
started a session itself holds the job id instead, and addresses the session
straight from the daemon roster, as `cx` does for a warm session. The handshake
in `open_lane` still checks whose REPL answers the socket, which is what stops a
recycled socket path from receiving the text.

The caller decides whether it may reach the job. This module only checks that
the job is alive and in a status where a paste queues as a prompt.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Callable, Optional

from awm.claudedaemon import roster, sessionmode
from awm.claudedaemon.lane import DaemonLane
from awm.claudedaemon.pty import DaemonError, Opener, open_lane, open_unix

#: The only statuses in which a paste queues as a prompt. Anything else (a
#: dialog waiting for an answer, a status this code has never seen) is refused.
SAFE_STATUSES = frozenset({"idle", "busy"})


class JobUnavailable(DaemonError):
    """The job is gone, unreachable, or at a prompt that a paste would answer."""


def lane_for(job: str, *, roster_path: Optional[Path] = None,
             jobs_dir: Optional[Path] = None,
             sessions_dir: Optional[Path] = None) -> DaemonLane:
    """The PTY lane of the live background session ``job``, or `JobUnavailable`.

    Paths default to the ones `sessionmode` resolves, so a cx override of the
    daemon home is seen here too.
    """
    roster_path = roster_path or sessionmode.roster_path()
    jobs_dir = jobs_dir or sessionmode.jobs_dir()
    sessions_dir = sessions_dir or sessionmode.sessions_dir()
    found = next((s for s in roster.load(roster_path, jobs_dir) if s.short == job), None)
    if found is None:
        raise JobUnavailable(f"job {job} is not in the daemon roster")
    if not found.repl_pid or not found.pty_sock or not found.pty_auth:
        raise JobUnavailable(f"job {job} has no reachable PTY")
    if roster.proc_start(found.repl_pid) != found.repl_proc_start:
        raise JobUnavailable(f"job {job} is no longer running")
    try:
        record = roster.read_session_record(found.repl_pid, sessions_dir=sessions_dir)
    except roster.SessionRecordError as exc:
        raise JobUnavailable(str(exc)) from None
    status = str(record.get("status") or "")
    if status not in SAFE_STATUSES:
        raise JobUnavailable(f"job {job} is {status or 'in an unknown state'}, not idle or busy")
    return DaemonLane(
        sock=found.pty_sock, auth=found.pty_auth, session_id=found.session_id or "",
        repl_pid=found.repl_pid, name=found.name, cli_version=found.cli_version,
        dec_modes=found.dec_modes,
    )


def send_line(job: str, text: str, *, roster_path: Optional[Path] = None,
              jobs_dir: Optional[Path] = None, sessions_dir: Optional[Path] = None,
              opener: Opener = open_unix,
              sleep: Callable[[float], None] = time.sleep) -> None:
    """Paste ``text`` into the session and press Enter. Raises `DaemonError` on failure.

    The text is a single line: a newline inside a bracketed paste would be kept
    as part of the prompt, so one is refused rather than silently rewritten.
    """
    if "\n" in text or "\r" in text:
        raise ValueError("send_line takes one line of text")
    lane = lane_for(job, roster_path=roster_path, jobs_dir=jobs_dir,
                    sessions_dir=sessions_dir)
    with open_lane(lane, opener=opener, sleep=sleep) as conn:
        conn.write(text)
        conn.commit()
