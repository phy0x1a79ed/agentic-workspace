"""Wait for an injected command to finish, by reading what the session says.

A self-directed slash command and its resume must never be co-queued: ``/compact``
runs at end of turn, whereas a resume is an ordinary message an *active* agent
consumes immediately — so co-queuing lets the resume run first, on the old
context, and starves the command of its idle slot. Something therefore has to
wait, and this is that something.

It waits on the session's own record and on nothing else. Claude Code writes
``~/.claude/sessions/<repl-pid>.json`` about itself, with the same fields whether
its pty belongs to a tmux pane or to the background daemon — so the waiting half
of reflection needs no transport knowledge at all, and this module deliberately
imports neither backend. That is not a tidiness argument. There were previously
two hand-written watchers, one per transport, and they drifted: the tmux one
would not deliver a resume until it had personally caught the session mid-turn on
a two-second screen poll, which a one-second turn simply never satisfies, so a
fast ``/compact`` stalled its resume for the full fifteen-minute cap. The daemon
one had already moved to the record and escaped that. One implementation cannot
drift from itself.

Reading the record rather than the screen also survives what screens do not. A
session that is attached, backgrounded, and reattached rewrites its record — the
hosting kind changes, the tmux field comes and goes — but keeps its pid, so a
wait keyed on the pid rides straight through the shuffle. A pane id does not.

The status vocabulary is closed: ``busy``, ``shell``, ``idle``, ``waiting``,
which is the full set the CLI will write (its own validator accepts no others).
Only ``idle`` is settled. ``shell`` is a tool call in flight. ``waiting`` is a
*blocking dialog* — a sandbox request, a permission prompt, "input needed" — and
resuming into one would paste a prompt behind a modal that swallows it, which is
the same failure ``guards.INTERACTIVE`` refuses on the injection side. Anything
unrecognised is treated as not-settled, so a vocabulary that grows leaves this
waiting rather than firing early.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Callable, Optional

from awm.reflection import session_target

log = logging.getLogger("awm.reflection.watcher")

# The complete set of statuses Claude Code writes (verified against the CLI's own
# validator, 2.1.233). Listed for the reader; the logic keys off SETTLED alone,
# so an unrecognised value keeps us waiting instead of firing.
KNOWN_STATUSES = ("busy", "shell", "idle", "waiting")

# Only a session doing nothing is done. See the module docstring for why
# `waiting` is not in here — it means a modal is open, not that the turn ended.
SETTLED = frozenset({"idle"})

POLL_S = 2.0
# Until the session is seen to react at all, poll tighter: that window is short
# and it is the one where a fast command could otherwise start and finish between
# two samples.
FAST_POLL_S = 0.3
# Consecutive settled samples before the resume fires. A measured compaction
# (interactive session, /compact injected mid-turn) held `busy` continuously from
# the driving turn through compaction and only then went `idle` — there is no
# idle beat between the two in the record, unlike on screen. So this streak is no
# longer load-bearing against that gap; it is kept at the value the daemon path
# has shipped without misfiring, as cheap insurance against a status flap.
SETTLE_POLLS = 3
# Hard cap. Past this the resume goes in anyway: a resume that arrives late beats
# a session that hangs idle forever, which is the failure this whole mechanism
# exists to prevent.
MAX_WAIT_S = 900.0

SETTLED_OUTCOME = "settled"
VANISHED = "vanished"
TIMED_OUT = "timed-out"


def read_status(repl_pid: int) -> Optional[tuple[str, int]]:
    """Return ``(status, statusUpdatedAt)`` from the session's record.

    ``None`` means the record could not be read *this time* — which is not the
    same as the session being gone, and callers must not treat it as such. The
    record is rewritten whenever a session is attached or backgrounded, and a
    read landing inside that is a transient miss.
    """
    try:
        record = json.loads(
            (session_target.SESSIONS_DIR / f"{repl_pid}.json").read_text())
    except (OSError, ValueError):
        return None
    return str(record.get("status") or ""), int(record.get("statusUpdatedAt") or 0)


def now_ms() -> int:
    return int(time.time() * 1000)


def await_completion(repl_pid: int, *, injected_at_ms: int,
                     label: str = "",
                     sleep: Callable[[float], None] = time.sleep,
                     clock: Callable[[], float] = time.monotonic,
                     proc_start: Callable[[int], Optional[str]] = None) -> str:
    """Block until the command injected at ``injected_at_ms`` has finished.

    Returns :data:`SETTLED_OUTCOME`, :data:`VANISHED` (the process is gone —
    there is nobody left to resume), or :data:`TIMED_OUT`.

    Two conditions must both hold. The session must have **reacted** — its
    status timestamp moved past the moment we injected — and it must then be
    **settled** for several consecutive samples. The reacted half is what fixes
    the stalled resume, and it is strictly better than watching for a busy
    *sighting*: a timestamp that has moved stays moved, so it cannot be missed
    between two polls, whereas a one-second turn between two two-second samples
    is invisible. Claude Code itself decides whether a session it kicked has
    responded the same way.

    Liveness is judged by the process, never by the record. A session being
    attached or backgrounded rewrites its record and can briefly make it
    unreadable, and reading that as "the session vanished" would drop a resume
    for a session that is merely being moved — precisely the case this has to
    ride through.
    """
    proc_start = proc_start or session_target._proc_start
    who = label or f"pid {repl_pid}"
    deadline = clock() + MAX_WAIT_S
    reacted = False
    settled_streak = 0
    unreadable = 0
    waiting_logged = False

    while clock() < deadline:
        sample = read_status(repl_pid)
        if sample is None:
            if proc_start(repl_pid) is None:
                log.warning("reflection: session %s is gone; no resume injected",
                            who)
                return VANISHED
            # Alive, but its record could not be read — it is most likely being
            # rewritten right now (an attach, a backgrounding). Keep waiting.
            unreadable += 1
            if unreadable in (1, 10):
                log.info("reflection: session %s is alive but its record is "
                         "briefly unreadable; still waiting", who)
            sleep(POLL_S if reacted else FAST_POLL_S)
            continue
        unreadable = 0
        status, updated_ms = sample
        if updated_ms > injected_at_ms:
            reacted = True
        if reacted and status in SETTLED:
            settled_streak += 1
            if settled_streak >= SETTLE_POLLS:
                return SETTLED_OUTCOME
        else:
            settled_streak = 0
            if status == "waiting" and not waiting_logged:
                # Worth one line: this is a session blocked on a dialog, and it
                # will stay blocked until a human answers. The resume waits for
                # it rather than being pasted behind the modal.
                log.info("reflection: session %s is waiting on a dialog; holding "
                         "its resume until that clears", who)
                waiting_logged = True
        sleep(POLL_S if reacted else FAST_POLL_S)

    log.warning("reflection: session %s did not settle within %ss (reacted=%s); "
                "injecting its resume anyway", who, MAX_WAIT_S, reacted)
    return TIMED_OUT
