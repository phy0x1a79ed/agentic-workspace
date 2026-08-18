"""Wait for an injected command to run, by reading what the session says.

A self-directed slash command and its resume must never be co-queued: ``/compact``
runs at end of turn, whereas a resume is an ordinary message an *active* agent
consumes immediately — so co-queuing lets the resume run first, on the old
context, and starves the command of its idle slot. Something therefore has to
wait, and this is that something.

What it waits for is the command **starting**, not finishing. Everything typed
into a session while it is compacting is accepted, queued, and drained into the
turn that begins the instant compaction ends — so a resume delivered any time
after compaction has begun runs first, on the fresh context, which is exactly
what it is for. Waiting for the session to look free instead cost a measured
27 seconds in the good case and, when the driving turn ran long, an hour of
holding followed by a give-up: a turn that keeps starting work freezes
``statusUpdatedAt`` and keeps the slash command queued, so the settled signal
could not arrive. The start line in the transcript can, and does, while the
session is at its busiest. Both signals are dated against the moment of
injection, because an earlier compaction left an identical start line behind.

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
``waiting`` is a *blocking dialog* — a sandbox request, a permission prompt,
"input needed" — and resuming into one would paste a prompt behind a modal that
swallows it, which is the same failure ``guards.INTERACTIVE`` refuses on the
injection side. Anything unrecognised is treated as not-settled, so a vocabulary
that grows leaves this waiting rather than firing early.

``shell`` is the one that cannot be read off the record alone, and treating it as
"a tool call in flight" is what made every autopilot resume wait out the cap. It
is also what a session shows while a *background* task runs — a Monitor, a
backgrounded Bash, a background Agent — and it keeps showing it after the turn
has ended, until that task finishes. An autopilot session nearly always has one,
so it is never seen ``idle``. The transcript is what tells the two apart: a
foreground call leaves an unanswered ``tool_use`` at the tail and a background
one does not (see :mod:`transcript`). So a ``shell`` that has sat unchanged for
:data:`SHELL_SETTLE_S`, with a clean transcript tail and nothing of ours left in
the queue, is a session between turns.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Callable, Optional

from awm.reflection import session_target, transcript

log = logging.getLogger("awm.reflection.watcher")

# The complete set of statuses Claude Code writes (verified against the CLI's own
# validator, 2.1.233). Listed for the reader; the logic keys off SETTLED alone,
# so an unrecognised value keeps us waiting instead of firing.
KNOWN_STATUSES = ("busy", "shell", "idle", "waiting")

# A session doing nothing is done, no corroboration needed. See the module
# docstring for why `waiting` is not in here — it means a modal is open, not that
# the turn ended — and for how `shell` earns the same verdict the long way round.
SETTLED = frozenset({"idle"})

# How long a `shell` must sit unchanged before the transcript gets a say. During
# a live turn the status flips between `busy` and `shell` every few seconds, so a
# frozen one is already good evidence that nothing is being driven; this only has
# to outlast the flapping, and stay well inside a compaction.
SHELL_SETTLE_S = 20.0

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
# Hard cap on one round of waiting. Reaching it is a *refusal to deliver*, not a
# licence to deliver anyway: a resume typed into a session that has not yet
# reached its command lands in the queue behind it, and would then run first, on
# the old context. The promise stays on disk instead and the wait is re-armed
# without limit — what the end of a round costs the session is a pause request
# (see ``inject._hold_and_nudge``), not its resume.
MAX_WAIT_S = 900.0

STARTED_OUTCOME = "started"
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


def is_compaction(text: str) -> bool:
    """Is ``text`` a ``/compact``? Decides whether a boundary is about it."""
    head = (text or "").strip().split()
    return bool(head) and head[0] == "/compact"


def has_started(tail, text: str, injected_at_ms: int) -> bool:
    """Has the command we injected begun running?

    The start line naming it is the direct answer, and a ``compact_boundary`` is
    the late one — a compaction that has *ended* has certainly begun, so a start
    line missed for any reason still resolves instead of hanging until the cap.
    Both are read as "since we typed", never as "present": an earlier compaction
    left the same two lines in the file.
    """
    if tail is None or not text:
        return False
    if tail.started(text, since_ms=injected_at_ms) is True:
        return True
    return is_compaction(text) and tail.compacted(since_ms=injected_at_ms) is True


def is_settled(status: str, updated_ms: int, tail, text: str = "") -> bool:
    """Is this session between turns, and therefore safe to type a resume into?

    ``idle`` says so outright. ``shell`` needs corroboration, because it is worn
    both by a session running a foreground tool and by one that finished its turn
    minutes ago with a Monitor still ticking — and only the second is safe. Three
    things must agree: the status has not moved for :data:`SHELL_SETTLE_S`, the
    transcript's tail holds no unanswered tool call, and nothing we typed is
    still sitting in the session's queue.

    Every transcript answer is tri-state, and only a definite ``False`` counts.
    An unreadable transcript leaves the session unsettled — waiting is always the
    safe direction here.
    """
    if status in SETTLED:
        return True
    if status != "shell":
        return False
    if now_ms() - updated_ms < SHELL_SETTLE_S * 1000:
        return False
    if tail is None or tail.tool_call_in_flight() is not False:
        return False
    return tail.queued(text) is not True


def await_completion(repl_pid: int, *, injected_at_ms: int,
                     label: str = "", text: str = "",
                     sleep: Callable[[float], None] = time.sleep,
                     clock: Callable[[], float] = time.monotonic,
                     proc_start: Callable[[int], Optional[str]] = None,
                     tail=None) -> str:
    """Block until the command injected at ``injected_at_ms`` is safe to resume past.

    Returns :data:`STARTED_OUTCOME` (the command is running, so anything typed
    now lands on the other side of it), :data:`SETTLED_OUTCOME` (the session is
    between turns), :data:`VANISHED` (the process is gone — there is nobody left
    to resume), or :data:`TIMED_OUT` (neither, after :data:`MAX_WAIT_S`, so the
    caller must hold its promise rather than deliver).

    :func:`has_started` is the signal that matters and the one that fires first.
    The settled path below it survives for the cases it cannot cover: a
    transcript that will not read, and a non-compacting slash command that leaves
    no boundary. That path needs two conditions. The session must have
    **reacted** — it took the command in — and it must then be :func:`settled
    <is_settled>` for several consecutive samples. Reacted is strictly better
    than watching for a busy *sighting*: a one-second turn between two
    two-second samples is invisible, whereas both signals here persist once true.

    A ``waiting`` status suppresses *both*: a blocking dialog owns the keyboard,
    so a resume typed then is swallowed by the modal rather than queued. The wait
    simply continues until a human answers it.

    ``text`` is the command that was injected, and it is only ever used as
    evidence about itself. ``tail`` is the transcript reader, which the caller
    should open *before* injecting so that nothing is missed.

    Liveness is judged by the process, never by the record. A session being
    attached or backgrounded rewrites its record and can briefly make it
    unreadable, and reading that as "the session vanished" would drop a resume
    for a session that is merely being moved — precisely the case this has to
    ride through.
    """
    proc_start = proc_start or session_target._proc_start
    who = label or f"pid {repl_pid}"
    if tail is None:
        tail = transcript.Tail(repl_pid)
    if text:
        tail.watch(text)
    deadline = clock() + MAX_WAIT_S
    reacted = False
    settled_streak = 0
    unreadable = 0
    waiting_logged = False

    while clock() < deadline:
        tail.poll()
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
        if status != "waiting" and has_started(tail, text, injected_at_ms):
            log.info("reflection: session %s has begun running the command it was "
                     "sent; its resume goes in now, to be taken up on the other "
                     "side of it", who)
            return STARTED_OUTCOME
        if updated_ms > injected_at_ms or (text and tail.consumed(text) is True):
            reacted = True
        if reacted and is_settled(status, updated_ms, tail, text):
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

    log.warning("reflection: session %s has not reached the command it was sent "
                "after %ss (reacted=%s); it is still owed its resume, which is "
                "being held rather than typed into a session that would only "
                "queue it", who, MAX_WAIT_S, reacted)
    return TIMED_OUT
