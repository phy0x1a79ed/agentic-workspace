"""The sender: detect the caller's lane, write into it, prove the write landed.

Callers reach reflection with nothing but what they want typed. Everything about
*where* that goes is derived from the caller's own process identity, which the
gateway observed rather than accepted as an argument. Two lanes sit behind this —
tmux for terminal sessions, the Claude Code daemon's PTY socket for background
ones — and which one runs is a detail of how the caller happens to be hosted, not
something they choose or can see.

The pipe is: **detect** (``session_target``) → **send** (here) → **wait**
(``watcher``) → detect → send again, for the resume. Each stage is one module,
and the lane is the value that flows between the first two.

Delivery is a short transaction rather than a blind write. Up to three times:
detect the lane *fresh*, sample the session's status, write the text, commit with
Enter, and then confirm from the session's own record that it took the line.
Detection sits inside the loop, immediately before the write, because the window
between deciding an address and using it is exactly where a session being
attached or backgrounded re-homes its pty out from under us — and that window
should be as close to zero as it can be made.

There is exactly one arbiter of whether a send landed, and it is not the screen.
The session's own record (``status``/``statusUpdatedAt``) and its transcript are
what every lane already reads, and they say the same thing whether the session
lives in a pane or in the background daemon. The screen used to gate the commit:
the text was read back off the lane and Enter was withheld unless it was visible.
That never worked on the daemon lane, which is a stream of the TUI's repaint
deltas — the TUI repaints the composer when it feels like it, and a paste it
chose not to paint was read as a paste that never arrived. It turned out not to
work on tmux either, for a reason no per-lane flag could fix: Claude Code does not
paint the composer *at all* while it is compacting, so a resume aimed at exactly
that window was written twelve times and submitted zero times (2026-09-07, across
a 94-second ``/compact``). A rendered screen renders what the app chose to draw,
never what it received. What can be checked on both lanes is what the *session*
did, and that only exists once Enter is on the wire.

Two things the screen gate was doing by accident are now done on purpose. A
session whose record reads ``waiting`` has a modal holding the keyboard, so the
attempt is refused *before* anything is typed rather than after a paste has been
left in a dialog. And the daemon lane's ``auth-required`` rejection frame, which
used to surface as a side effect of reading the screen, is an explicit
``check_not_rejected`` on the writer protocol — real on the daemon lane, a no-op
on the others, so the sender still cannot tell which lane it is holding.

Enter is still the retry boundary, but the record now says which side of it we
are on. A session that was settled when we wrote and never moved consumed
nothing, so a retry is provably free; a session that was mid-turn queued the line
and there is nothing to observe, so nothing is claimed. Attempts two and three
clear the prompt first, which is what makes a retry idempotent: a half-landed
paste from the previous attempt is wiped rather than concatenated onto. Attempt
one does not clear, so an ordinary successful send never destroys anything a
human may have been mid-way through typing — and a final give-up clears on the
way out, because by then the only thing in the box is ours.

None of this establishes that the command *ran*. It establishes that the session
consumed the keystrokes. The difference is structural and permanent — see
``awm.claudedaemon``'s module docstring.
"""
from __future__ import annotations

import logging
import threading
import time
import uuid
from dataclasses import replace
from typing import Any, Callable, NamedTuple, Optional

from awm import claudedaemon
from awm.claudedaemon import roster
from awm.cch import (
    events,
    guards,
    observation,
    pending,
    session_target,
    tmux_inject,
    watcher,
)

log = logging.getLogger("awm.cch.inject")

# Three tries, as agreed. Enough to cross a single re-host plus one unlucky beat;
# not so many that a genuinely unreachable session is ground against while the
# caller blocks on the verb.
ATTEMPTS = 3

# Pause between attempts. This loop used to run flat out, so three attempts at a
# session that was briefly unreachable were spent inside 2.4 seconds and the
# whole retry budget bought nothing that one attempt would not have.
RETRY_BACKOFF_S = 0.5

# How long the session's own record gets to show that it took our Enter, and how
# often to look. Measured on a scratch background session: `statusUpdatedAt`
# moved 102ms after the submit. Three seconds is thirty times that, and it is the
# cap `permission_mode`'s read has shipped with.
CONFIRM_WAIT_S = 3.0
CONFIRM_POLL_S = 0.05

# How a submit came to be believed. Reported to the caller, because "we could not
# tell" and "it went in" are different answers and the caller acts on them
# differently.
CONFIRMED_RECORD = "record"        # the session's record moved: it took the line
CONFIRMED_ENQUEUED = "enqueued"    # mid-turn, and its transcript shows ours queued
CONFIRMED_QUEUED = "queued"        # mid-turn at write time; nothing observable
CONFIRMED_BLOCKED = "blocked"      # a modal had the keyboard; probably swallowed
CONFIRMED_UNREADABLE = "unreadable"  # record unreadable throughout; no claim
NOT_SUBMITTED = "none"             # `enter` was false, so nothing was submitted

# How long to watch a session's transcript for proof that a resume it was sent
# actually arrived, and how often to look. Generous next to the ~100ms a settled
# session takes to react, because this runs detached and costs nobody's turn.
VERIFY_WAIT_S = 15.0
VERIFY_POLL_S = 0.5

# How many times a *delivery* may fail before the promise is left for a human.
# This bounds the one failure that grinding cannot fix: a lane that will not take
# the text, or a session whose transcript never shows it arriving.
#
# Waiting is deliberately not bounded. It used to be — four rounds, an hour — and
# a session whose driving turn ran 63 minutes had its promise abandoned three
# minutes before its `/compact` even began, then sat idle with the resume it was
# owed still on disk. A session that is alive and has not yet reached its command
# is the mechanism working, however long it takes; the answer to a long wait is
# to ask the session to pause (see :func:`_hold_and_nudge`), not to give up on it.
MAX_DELIVERY_ROUNDS = 4

# How long to wait before re-arming after a *delivery* round failed, indexed by
# how many have failed so far (the last value repeats).
#
# The numbers are sized against a real compaction. On 2026-09-07 a `/compact` ran
# for 94 seconds and its resume failed four times in 7.7 — the loop re-entered
# the wait, the wait saw the command's start line still sitting in the transcript
# and returned "started" immediately, and the entire delivery budget was spent
# inside the first eight seconds of a window a minute and a half wide. Whatever
# is wrong with a lane at the moment a round fails, nothing about it changes in
# 40ms; the only thing worth spending a round on is a *later* moment. Escalating
# rather than flat because the second failure is weaker evidence that waiting
# helps than the first, and 30+60+120 carries the last round well past the
# longest compaction observed.
ROUND_BACKOFF_S = (30.0, 60.0, 120.0)

# Claude Code 2.1.302 drops a session's MCP servers when `/cd` moves it and does
# not connect them again, whatever directory it lands in. A session that moved
# itself would come back without awm, and so without this service, before it
# could ask for it, so the resume after a move is preceded by this command.
RECONNECT_AFTER_MOVE = guards.mcp_line("reconnect", "awm")

# How long the resume after a self-directed `/mcp` waits for the command's output
# to reach the transcript. A reconnect prints once the server has answered.
MCP_OUTPUT_WAIT_S = 30.0
MCP_OUTPUT_POLL_S = 0.5

# How many times a session will be asked to bring its turn to a close before the
# wait goes quiet and simply keeps holding. A pause request is text arriving
# unbidden in somebody's session, so it is rationed: three across the life of a
# promise, at most one per fifteen-minute round.
NUDGE_LIMIT = 3

# Claude Code's daemon swaps an outdated background worker for a new process in
# place, and a swap during `/compact` loses the compaction and everything queued
# behind it (compaction-survey.md, 2026-11-03: 13 of 545, all on outdated
# workers, the new process 3 to 51 s after the command began). A dead pid is
# therefore not the end of a promise: the session id is looked up under a new
# pid for this long, and the command or the resume typed again at most
# MAX_REISSUES times.
SUCCESSOR_WAIT_S = 120.0
SUCCESSOR_POLL_S = 2.0
MAX_REISSUES = 2

# How long after the resume is typed the session gets to show it taken up, and
# for `/compact` the `compact_boundary`. The longest compaction measured ran 94 s.
FOLLOW_THROUGH_S = 1800.0
FOLLOW_POLL_S = 2.0

# What a follow-through ended in.
_DONE = "done"
_REARM = "rearm"
_LOST = "lost"

# Statuses that say outright that typing now would land in a queue or behind a
# modal. `shell` is not among them — see :func:`watcher.is_settled`.
_NEVER_SETTLED = ("busy", "waiting")


class Delivery(NamedTuple):
    """What a delivery achieved: whether it submitted, where, and on what evidence."""
    submitted: bool
    lane: Any
    confirmed: str


class DeliveryError(RuntimeError):
    """Every attempt to put the text into the calling session failed."""


class CommitFailed(DeliveryError):
    """Enter was sent, and something went wrong at or after that point.

    Deliberately *not* retryable, which is why it is raised as a
    :class:`DeliveryError` rather than one of the lane failures the retry loop
    catches. Everything before the commit is safe to repeat because nothing was
    submitted; once Enter is on the wire the command may already be running, and
    ``send`` carries arbitrary text — running it twice is a worse outcome than
    not running it at all.
    """


class SessionBlocked(RuntimeError):
    """A modal dialog holds the session's keyboard, so nothing was typed.

    Retryable, and refused *before* the write rather than after it: a paste into
    an open dialog is consumed by the dialog, and a retry then concatenates onto
    whatever the dialog did with it. The old screen gate caught this case by
    accident — the composer is not painted behind a modal — and that accident is
    the only thing the gate was contributing on the tmux lane, so it is kept as
    an explicit check on the one signal that says so outright.
    """


class NotSubmitted(RuntimeError):
    """Enter was pressed and the session's own record proves it consumed nothing.

    Retryable, and the only thing past the commit point that is. The guarantee is
    positive rather than assumed: a session that never left ``idle`` and whose
    status timestamp never moved did not start a turn, so there is no command
    half-running that a repeat could double-submit. That is a stronger licence to
    retry than the old pre-Enter boundary had, which only knew that *we* had not
    pressed Enter yet.
    """


def _resolve(caller_pid: Optional[int]):
    if not caller_pid:
        raise session_target.ResolveError(
            "could not tell which session is calling, so there is nothing safe "
            "to inject into. Reflection types into the caller's own prompt and "
            "will not guess at a target — this usually means the call did not "
            "come through a session's own `awm mcp` proxy (a plain shell, for "
            "instance)")
    return resolve_lane(caller_pid)


def resolve_lane(caller_pid: int):
    """The caller's injection target: a tmux pane or a background session's PTY."""
    return session_target.resolve(caller_pid)


# ---------------------------------------------------------------------------
# Confirmation — the sole arbiter
# ---------------------------------------------------------------------------

def _confirm_submit(repl_pid: int, before: Optional[tuple[str, int]], *,
                    text: str = "", tail=None,
                    sleep: Callable[[float], None] = time.sleep,
                    clock: Callable[[], float] = time.monotonic,
                    read_status=None) -> str:
    """Did the session take the Enter we just pressed? Ask its own record.

    This is the transport-independent half, and deliberately the *same* signal
    the watcher waits on: Claude Code writes ``status``/``statusUpdatedAt`` about
    itself whichever pty it is hosted on, so tmux and daemon sessions are
    confirmed by one code path that cannot drift between them.

    The record is written on **transitions**, not on activity, which decides the
    whole shape here. A session that was settled when we wrote has to move to run
    our line, so its timestamp advancing is proof it did — and a timestamp that
    has moved stays moved, so a fast turn cannot slip between two polls. Settled
    means what :func:`watcher.is_settled` means, not "``idle``": a session held at
    ``shell`` by a background task runs our line immediately, and reporting that
    as queued would understate a delivery that in fact went straight in. A
    A session that was already mid-turn *queues* the line instead, and queuing
    moves nothing — so the record has nothing to say about it. Its transcript
    does: an ``enqueue`` naming our text is the session acknowledging the
    keystrokes, and that is :data:`CONFIRMED_ENQUEUED`. Only when there is no
    transcript to read does this fall back to :data:`CONFIRMED_QUEUED`, which is
    an assumption and is named apart from the observation for that reason.

    Returns :data:`NOT_SUBMITTED` only for the proven negative: settled
    throughout, timestamp never moved.
    """
    read_status = read_status or watcher.read_status
    if before is None:
        # No reference to compare against, so "did it move?" has no answer. Not
        # the same thing as the session being busy, and not the same thing as a
        # failure either.
        return CONFIRMED_UNREADABLE
    if before[0] == "waiting":
        # A blocking dialog owns the keyboard, so the paste was most likely eaten
        # by it rather than queued behind a turn — the same failure
        # `guards.INTERACTIVE` refuses on the way in. Named apart from `queued`
        # because the two want opposite things from whoever reads the result.
        # `_attempt` now refuses this state *before* writing, so on that path
        # this branch is unreachable; it stays for callers that confirm a submit
        # they did not make through `_attempt`, and as the honest answer when a
        # session opened a dialog between the sample and the Enter.
        return CONFIRMED_BLOCKED
    if not watcher.is_settled(before[0], before[1], tail, text):
        deadline = clock() + CONFIRM_WAIT_S
        while tail is not None and text and clock() < deadline:
            tail.poll()
            if tail.landed(text) is True:
                return CONFIRMED_ENQUEUED
            sleep(CONFIRM_POLL_S)
        return CONFIRMED_QUEUED
    deadline = clock() + CONFIRM_WAIT_S
    ever_read = False
    while clock() < deadline:
        sample = read_status(repl_pid)
        if sample is not None:
            ever_read = True
            if sample[1] > before[1]:
                return CONFIRMED_RECORD
        sleep(CONFIRM_POLL_S)
    # An unreadable record is not a negative — it is rewritten on every attach
    # and backgrounding, and the watcher rides through the same gap rather than
    # calling the session gone.
    return NOT_SUBMITTED if ever_read else CONFIRMED_UNREADABLE


# ---------------------------------------------------------------------------
# The transaction
# ---------------------------------------------------------------------------

def _open_lane(lane, **kw):
    if isinstance(lane, session_target.DaemonLane):
        return claudedaemon.open_lane(
            lane, **{k: v for k, v in kw.items() if k in ("opener", "sleep")})
    return tmux_inject.open_lane(
        lane, **{k: v for k, v in kw.items()
                 if k in ("socket", "runner", "sleep")})


_LANE_FAILURES = (session_target.ResolveError,
                  tmux_inject.TmuxError, claudedaemon.DaemonError,
                  SessionBlocked, NotSubmitted, OSError)


def _attempt(repl_pid: int, text: str, *, enter: bool, clear_first: bool,
             detect, read_status=None, tail=None, **kw) -> Delivery:
    """One detect → guard → write → commit → confirm transaction. Raises on failure.

    Nothing rendered is consulted anywhere in here. The only thing asked before
    the write is the session's own record, and it is asked one question — is a
    modal holding the keyboard — because that is the one state in which typing
    does something other than queue. Everything else is settled *after* Enter, by
    what the session did, which is the same question on every lane.
    """
    lane = detect(repl_pid)
    # Sampled before anything is typed, because it serves twice: it is the modal
    # guard below, and it is the reference the confirmation compares against —
    # "did this move?" needs a from.
    was = (read_status or watcher.read_status)(repl_pid)
    if was is not None and was[0] == "waiting":
        raise SessionBlocked(
            f"session {lane.name or lane.session_id} is at a prompt waiting for "
            f"an answer, so a paste would be eaten by the dialog rather than "
            f"queued; nothing was typed")
    with _open_lane(lane, **kw) as writer:
        if clear_first:
            writer.clear()
        if tail is not None:
            tail.expect(text)
        writer.write(text)
        # The one thing a lane can say about a write it just took: not that it
        # arrived, only that the host did not announce discarding it.
        writer.check_not_rejected()
        if not enter:
            return Delivery(False, lane, NOT_SUBMITTED)
        try:
            writer.commit()
        except Exception as exc:
            raise CommitFailed(
                f"the text reached {writer.label} and Enter was sent, but the "
                f"submit did not complete cleanly ({exc}); not retrying, "
                f"because it may already be running") from None
    confirmed = _confirm_submit(repl_pid, was, text=text, tail=tail,
                                read_status=read_status,
                                sleep=kw.get("sleep", time.sleep))
    if confirmed == NOT_SUBMITTED:
        raise NotSubmitted(
            f"Enter was sent to {lane.kind} session {lane.name or lane.session_id} "
            f"but it stayed settled with its status unchanged for "
            f"{CONFIRM_WAIT_S}s, so it consumed nothing — the paste did not "
            f"reach its prompt")
    return Delivery(True, lane, confirmed)


def _wipe_prompt(repl_pid: int, detect, **kw) -> None:
    """Best-effort: leave nothing of a failed send in the session's prompt.

    A give-up used to leave exactly one unsubmitted paste in the box — the last
    attempt's, since only attempts two and three clear on the way *in*. Whatever
    types next then concatenates onto it, or a stray Enter submits it minutes
    later with nobody expecting it. By the time we are here the only thing that
    can be in there is ours, so clearing it is safe in a way that clearing on the
    way in is not.
    """
    try:
        with _open_lane(detect(repl_pid), **kw) as writer:
            writer.clear()
    except Exception as exc:  # noqa: BLE001 - a cleanup must not mask the failure
        log.warning("reflection: could not clear the prompt of pid %s after a "
                    "failed send; an unsubmitted paste may be left in it: %s",
                    repl_pid, exc)


def deliver(repl_pid: int, text: str, *, enter: bool = True,
            attempts: int = ATTEMPTS, detect=None, what: str = "text",
            tail=None, **kw) -> Delivery:
    """Put ``text`` into session ``repl_pid``, or raise :class:`DeliveryError`.

    Returns a :class:`Delivery` — whether it submitted, the lane the write went
    to (which the caller needs for its result and its promise), and what the
    submit was confirmed by.

    Every attempt and every failure is logged. A send that only worked on the
    third try is the system telling you something about this host, and it should
    be readable later without anyone re-deriving it from timestamps.
    """
    detect = detect or resolve_lane
    sleep = kw.get("sleep", time.sleep)
    failures: list[str] = []
    blocked = False
    for attempt in range(1, attempts + 1):
        if attempt > 1:
            sleep(RETRY_BACKOFF_S)
        try:
            result = _attempt(repl_pid, text, enter=enter,
                              clear_first=attempt > 1, detect=detect,
                              tail=tail, **kw)
        except CommitFailed as exc:
            log.error("reflection: %s reached pid %s on attempt %s/%s but the "
                      "submit did not complete: %s", what, repl_pid, attempt,
                      attempts, exc)
            raise
        except _LANE_FAILURES as exc:
            blocked = isinstance(exc, SessionBlocked)
            failures.append(f"attempt {attempt}: {exc}")
            log.warning("reflection: attempt %s/%s to deliver %s to pid %s "
                        "failed: %s", attempt, attempts, what, repl_pid, exc)
            continue
        if attempt > 1:
            log.warning("reflection: delivered %s to pid %s on attempt %s/%s "
                        "via the %s lane, confirmed by %s (earlier attempts "
                        "failed — %s)", what, repl_pid, attempt, attempts,
                        result.lane.kind, result.confirmed, "; ".join(failures))
        else:
            log.info("reflection: delivered %s to pid %s via the %s lane, "
                     "confirmed by %s", what, repl_pid, result.lane.kind,
                     result.confirmed)
        return result
    log.error("reflection: giving up on delivering %s to pid %s after %s "
              "attempts — %s", what, repl_pid, attempts, "; ".join(failures))
    if not blocked:
        # The wipe is safe because by here the only thing that can be in the box
        # is ours. A session still behind a modal on the last attempt was never
        # written to at all, and a Ctrl-U aimed at its prompt goes to the dialog
        # instead — answering somebody's question for them. Leave it alone.
        _wipe_prompt(repl_pid, detect, **kw)
    raise DeliveryError(
        f"could not deliver {what} to the calling session after {attempts} "
        f"attempts: " + "; ".join(failures))


# ---------------------------------------------------------------------------
# Verbs
# ---------------------------------------------------------------------------

def _default_spawn(fn: Callable[[], None]) -> Any:
    return threading.Thread(target=fn, name="reflection-followup",
                            daemon=True).start()


def send(text: str, *, caller_pid: Optional[int], enter: bool = True,
         delay_ms: int = 0, confirm: bool = False,
         followup: Optional[str] = None, allow_modal: bool = False,
         change: Optional[pending.Change] = None,
         spawn: Callable[[Callable[[], None]], Any] = _default_spawn,
         **kw) -> dict:
    """Inject ``text`` into the calling session, whatever is hosting it.

    ``change`` names the settings command ``text`` is, so the resume can say
    what became of it. A session keeps one promise, so a command sent while an
    earlier one is still owed a resume takes that promise over, carrying the
    earlier changes with it.
    """
    if not caller_pid:
        raise session_target.ResolveError(
            "could not tell which session is calling, so there is nothing safe "
            "to inject into. Reflection types into the caller's own prompt and "
            "will not guess at a target — this usually means the call did not "
            "come through a session's own `awm mcp` proxy (a plain shell, for "
            "instance)")
    # Ask the guards before announcing or detecting anything. This used to run
    # after the log line, which made the log claim a refused `/clear` had gone
    # in — and on the one path where someone is trying to find out why a command
    # did not run, a log that overstates what happened is worse than no log.
    refused = guards.refusal(text, confirm=confirm, allow_modal=allow_modal)
    if refused is not None:
        return refused

    if delay_ms and delay_ms > 0:
        time.sleep(min(delay_ms, 25_000) / 1000.0)

    # Opened before a single keystroke goes out, so that whatever the session
    # does with the text is inside the window this reader can see.
    obs = observation.observation_for(caller_pid)
    tail = obs.open_tail(caller_pid)
    tail.watch(text)
    tail.poll()

    injected_at_ms = watcher.now_ms()
    # A failure propagates: the caller is told the command did not go in, and —
    # because this sits above the record-and-spawn below — no promise is left
    # behind for a resume to a command that was never injected.
    result = deliver(caller_pid, text, enter=enter,
                     what="the requested command", tail=tail,
                     read_status=obs.read_status, **kw)
    submitted, lane = result.submitted, result.lane

    followup_sent = None
    followup_deferred = False
    if submitted and guards.is_slash(text):
        followup_deferred = True
        proc_start = session_target._proc_start(lane.repl_pid) or ""
        earlier = pending.read(lane.repl_pid)
        if earlier is not None and earlier.proc_start != proc_start:
            earlier = None
        changes = (earlier.changes if earlier else ()) + (
            (change,) if change else ())
        closing = (followup or "").strip() or (earlier.closing if earlier else "")
        followup_sent = pending.render_followup(changes, None, closing)
        promise = pending.Pending(
            repl_pid=lane.repl_pid, proc_start=proc_start,
            session_id=lane.session_id, text=text, followup=followup_sent,
            injected_at_ms=injected_at_ms, name=lane.name, hosting=lane.hosting,
            changes=changes, closing=closing,
            ticket=(earlier.ticket if earlier and earlier.ticket
                    else uuid.uuid4().hex))
        # Written down BEFORE the watcher starts: the watcher is a thread in this
        # process, and the gateway restarts this process out from under it.
        pending.record(promise)
        # `read_status` goes with `tail`: both were built above from
        # `observation_for`, and the resume reads them as one pair.
        resume_watch(promise, spawn=spawn, tail=tail,
                     read_status=obs.read_status, **kw)

    out = {"ok": True, "session": lane.name or lane.session_id,
           "hosting": lane.hosting, "text": text, "submitted": submitted,
           # What the submit rests on. For the commonest call of all — a session
           # compacting itself — the session is by definition mid-turn, so its
           # line waits behind the turn that asked for it and its record will not
           # move until that turn ends. `enqueued` is that case *observed* in the
           # session's transcript; `queued` is the same case with no transcript to
           # read, and is inference rather than evidence.
           "confirmed": result.confirmed,
           "followup": followup_sent, "followup_deferred": followup_deferred}
    if followup_deferred:
        out["ticket"] = promise.ticket
    if isinstance(lane, session_target.TmuxLane):
        out["pane"] = lane.pane
    return out


def _free_to_receive(repl_pid: int, tail, read_status=None) -> bool:
    """Is anything visibly wrong with typing into this session right now?

    The watcher has just said it settled, but a wait and a write are two
    different moments and a session can start a turn in the gap. This is the
    last-instant re-check, and it refuses only on what it can positively see —
    a `busy` or `waiting` status, or a foreground tool call outstanding. The
    watcher has already done the careful work; re-running its whole test here
    would throw away a fifteen-minute round over a record that happened to be
    mid-rewrite.
    """
    read_status = read_status or watcher.read_status
    sample = read_status(repl_pid)
    if sample is not None and sample[0] in _NEVER_SETTLED:
        return False
    tail.poll()
    return tail.tool_call_in_flight() is not True


def _verify_landed(tail, text: str, *, sleep: Callable[[float], None] = time.sleep,
                   clock: Callable[[], float] = time.monotonic) -> Optional[bool]:
    """Did ``text`` reach the session? Read it out of the session's transcript.

    ``True`` if the session took it in or queued it, ``False`` if the transcript
    was read in this window and never mentioned it, ``None`` if no read in the
    window succeeded. Only ``False`` is evidence of a failure; ``None`` is the
    absence of evidence and must not license a second delivery. A tail that read
    the file earlier and has lost it since still answers from those old reads,
    so its answer counts only if some poll here got through.
    """
    deadline = clock() + VERIFY_WAIT_S
    read = False
    while True:
        read = tail.poll() or read
        if tail.landed(text) is True:
            return True
        if clock() >= deadline:
            break
        sleep(VERIFY_POLL_S)
    return tail.landed(text) if read else None


def _nudge_text(item: pending.Pending, waited_s: int) -> str:
    """The pause request. One line, and unmistakably not a new instruction.

    It arrives mid-turn in somebody's session, unasked for, so it says who sent
    it and asks for exactly one thing — a turn boundary. Anything that reads like
    work would be work the session did not choose to do. It goes in as plain
    text, never a slash command, and that asymmetry is the whole reason it lands:
    a queued slash command waits for a turn boundary, while a plain prompt is
    handed to the turn that is running (measured: seven such messages enqueued
    and delivered within seconds each, across the 63 minutes one session's
    `/compact` sat untouched in the same queue).
    """
    return (
        f"[awm reflection] Your queued `{item.text}` cannot run until the current "
        f"turn ends, and it has been waiting {waited_s // 60} minutes. Please "
        f"finish the step you are on and end your turn without starting anything "
        f"new — `{item.text}` will run then, and reflection will send you "
        f"\"{item.followup}\" straight afterwards, so nothing is lost by stopping "
        f"here. Automated message from the reflection service; it is not a new "
        f"instruction from the user.")


def _hold_and_nudge(item: pending.Pending, *, tail=None, read_status=None,
                    now_ms=None, **kw) -> pending.Pending:
    """Keep the promise for another round, and ask the session to pause.

    Called when a round ended with the command still not run. Holding is silent
    by itself, and silence is how an hour goes by: the session has no idea it is
    sitting on its own compaction, and the only thing that ever broke that
    deadlock was a human typing "pause for a moment to let the compact message
    through" by hand.
    """
    who = item.name or item.session_id or f"pid {item.repl_pid}"
    reason = f"still waiting for {item.text} to run (round {item.holds + 1})"
    if item.nudges >= NUDGE_LIMIT:
        item = pending.held(item, f"{reason}; asked {item.nudges} times already, "
                                  f"now holding quietly")
        pending.record(item)
        return item

    waited_s = max(0, ((now_ms or watcher.now_ms()) - item.injected_at_ms) // 1000)
    text = _nudge_text(item, waited_s)
    if tail is not None:
        tail.watch(text)
    try:
        deliver(item.repl_pid, text, enter=True, tail=tail,
                read_status=read_status, what="a pause request", **kw)
    except DeliveryError as exc:
        # A nudge is a courtesy, not the promise. Failing to deliver one must not
        # spend the resume's own delivery budget.
        log.warning("reflection: could not ask session %s to pause; still "
                    "holding its resume: %s", who, exc)
        item = pending.held(item, f"{reason}; the pause request could not be "
                                  f"delivered")
        pending.record(item)
        return item
    item = pending.held(pending.nudged(item),
                        f"{reason}; asked the session to pause")
    pending.record(item)
    return item


_ACTIVE: dict[int, list[int]] = {}
_ACTIVE_LOCK = threading.Lock()


def _superseded(item: pending.Pending) -> bool:
    """Has a newer promise replaced ``item`` for its session?

    A session owes one resume. Each self-directed slash command records a promise
    and starts a watcher, and the file holds only the newest, so an older watcher
    that carried on would deliver a second resume and clear the first's record.
    """
    with _ACTIVE_LOCK:
        if any(ms > item.injected_at_ms for ms in _ACTIVE.get(item.repl_pid, ())):
            return True
    if pending.delivered_ms(item.repl_pid) > item.injected_at_ms:
        return True
    on_disk = pending.read(item.repl_pid)
    return on_disk is not None and on_disk.injected_at_ms != item.injected_at_ms


def _rc_link(repl_pid: int) -> Optional[str]:
    try:
        job = roster.job_of_pid(repl_pid, sessions_dir=session_target.SESSIONS_DIR)
        bridge = (roster.job_bridge_id(job, session_target.JOBS_DIR) if job
                  else roster.pid_bridge_id(
                      repl_pid, sessions_dir=session_target.SESSIONS_DIR))
        return roster.bridge_link(bridge)
    except Exception as exc:  # noqa: BLE001 - the link is a nicety
        log.warning("reflection: could not read the Remote Control link of pid "
                    "%s: %s", repl_pid, exc)
        return None


def _mcp_outputs(item: pending.Pending, tail, *,
                 sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic) -> pending.Pending:
    """``item`` with each `/mcp` change carrying what the command printed."""
    owed = [c for c in item.changes if c.kind == "mcp" and not c.detail]
    if not owed:
        return item
    deadline = clock() + MCP_OUTPUT_WAIT_S
    said: dict[str, str] = {}
    while True:
        tail.poll()
        for c in owed:
            out = tail.output(f"/mcp {c.value}", since_ms=item.injected_at_ms)
            if out is not None:
                said[c.key] = out
        if len(said) == len(owed) or clock() >= deadline:
            break
        sleep(MCP_OUTPUT_POLL_S)
    return replace(item, changes=tuple(
        replace(c, outcome=pending.APPLIED, detail=said[c.key]) if c.key in said else c
        for c in item.changes))


def _resume_text(item: pending.Pending, tail=None, **kw) -> pending.Pending:
    """``item`` with the resume rendered from what the changes did by now."""
    if not item.changes:
        return item
    on_disk = pending.read(item.repl_pid)
    if on_disk is not None and on_disk.injected_at_ms == item.injected_at_ms:
        item = replace(item, changes=on_disk.changes)
    if tail is not None:
        item = _mcp_outputs(item, tail, **{k: kw[k] for k in ("sleep", "clock") if k in kw})
    link = (_rc_link(item.repl_pid)
            if any(c.kind == "rc_on" for c in item.changes) else None)
    return replace(item, followup=pending.render_followup(
        item.changes, link, item.closing))


def _await_and_resume(item: pending.Pending, *, tail=None, read_status=None,
                      **kw) -> None:
    """Wait for the command to run, then deliver the resume — and prove it.

    Runs detached — a synchronous wait would deadlock, because the caller's turn
    has to end for the queued slash command to run at all.

    A resume goes in once the command is *running* (anything typed then is
    drained into the turn that starts on the other side of it) or once the
    session is between turns. It is never typed into a session that has not
    reached the command yet: that puts it in the queue ahead of nothing and
    behind the command, where it runs first, on the old context — a stray prompt
    rather than a resume. Such a session keeps its promise and is waited on
    again, without limit while it is alive, and is asked to pause once a round.
    Only a *delivery* that keeps failing is bounded, by
    :data:`MAX_DELIVERY_ROUNDS`.
    """
    who = item.name or item.session_id or f"pid {item.repl_pid}"
    if tail is None or read_status is None:
        # Keyed on either being absent, not on `tail` alone. The boot-time replay
        # arrives with neither; `send` arrives with both. A caller that passes one
        # and not the other used to get the Claude reader by default whatever
        # harness it was watching.
        obs = observation.observation_for(item.repl_pid)
        tail = tail if tail is not None else obs.open_tail(item.repl_pid)
        read_status = read_status if read_status is not None else obs.read_status
    tail.watch(item.text)
    tail.watch(item.followup)

    with _ACTIVE_LOCK:
        _ACTIVE.setdefault(item.repl_pid, []).append(item.injected_at_ms)
    try:
        _await_and_resume_inner(item, tail, who, read_status=read_status, **kw)
    finally:
        with _ACTIVE_LOCK:
            _ACTIVE[item.repl_pid].remove(item.injected_at_ms)
            if not _ACTIVE[item.repl_pid]:
                del _ACTIVE[item.repl_pid]


def _round_backoff(failures: int) -> float:
    """How long to hold off before re-arming, after ``failures`` failed rounds."""
    if failures <= 0:
        return 0.0
    return ROUND_BACKOFF_S[min(failures, len(ROUND_BACKOFF_S)) - 1]


def _alive(item: pending.Pending) -> bool:
    started = session_target._proc_start(item.repl_pid)
    return started is not None and (not item.proc_start or started == item.proc_start)


def _await_successor(item: pending.Pending, who: str, *,
                     sleep: Callable[[float], None] = time.sleep,
                     clock: Callable[[], float] = time.monotonic,
                     **_kw) -> Optional[pending.Pending]:
    """``item`` moved to the process that took over its session, or None."""
    deadline = clock() + SUCCESSOR_WAIT_S
    while True:
        found = session_target.successor(item.session_id, item.repl_pid)
        if found is not None:
            pid, started = found
            log.warning("reflection: session %s moved from pid %s to pid %s, a "
                        "worker swap; following it", who, item.repl_pid, pid)
            moved = replace(item, repl_pid=pid, proc_start=started)
            pending.rekey(item.repl_pid, moved)
            return moved
        if clock() >= deadline:
            return None
        sleep(SUCCESSOR_POLL_S)


def _command_survived(tail, item: pending.Pending) -> bool:
    """Did the command finish before its worker was swapped out?

    A start line is not enough here: a swap during `/compact` leaves the start
    line and loses the compaction. Only the boundary says it finished.
    """
    tail.poll()
    if watcher.is_compaction(item.text):
        return tail.compacted(since_ms=item.injected_at_ms) is True
    return tail.started(item.text, since_ms=item.injected_at_ms) is True


def _reissue(item: pending.Pending, tail, who: str, *, read_status=None,
             **kw) -> pending.Pending:
    """Type the command again into the session's new worker. Raises DeliveryError."""
    now = watcher.now_ms()
    tail.rebind(item.repl_pid)
    tail.watch(item.text)
    log.warning("reflection: typing %s again into session %s, whose worker was "
                "swapped before it finished (re-issue %s of %s)", item.text, who,
                item.reissues + 1, MAX_REISSUES)
    deliver(item.repl_pid, item.text, enter=True, tail=tail,
            read_status=read_status,
            what="the command again after a worker swap", **kw)
    item = replace(item, injected_at_ms=now, phase=pending.AWAITING_COMMAND,
                   reissues=item.reissues + 1,
                   last_outcome="typed again after a worker swap")
    pending.record(item)
    return item


def _lost(item: pending.Pending, reason: str, *, alive: bool, **kw) -> None:
    """Forget the promise and say out loud that it was lost."""
    pending.clear(item.repl_pid)
    events.lost(
        item, reason, alive_pid=item.repl_pid if alive else None,
        deliver=lambda pid, text, **k: deliver(pid, text, **{**kw, **k}))


def _follow_through(item: pending.Pending, tail, who: str, *, read_status=None,
                    **kw) -> tuple[str, pending.Pending]:
    """Hold the promise until the session has taken the resume up.

    Typed is not delivered. The resume waits in the worker's memory until the
    command ends, and a worker swap in that window loses both. So the promise
    stays on disk until the transcript shows the resume taken out of the queue
    and, for `/compact`, the `compact_boundary`. A swap in the window sends the
    command or the resume again into the new worker.
    """
    sleep = kw.get("sleep", time.sleep)
    clock = kw.get("clock", time.monotonic)
    compaction = watcher.is_compaction(item.text)
    tail.watch(item.text)
    tail.watch(item.followup)
    deadline = clock() + FOLLOW_THROUGH_S
    while True:
        tail.poll()
        if _superseded(item):
            events.done(item, "a newer command for the session replaced it")
            return _DONE, item
        took = tail.taken_up(item.followup, since_ms=item.injected_at_ms)
        if took is None:
            # No transcript to read, now or ever: nothing can confirm or refute
            # it, and typing the resume twice is the worse mistake.
            pending.clear(item.repl_pid)
            events.done(item, "typed; the session has no transcript to confirm it")
            return _DONE, item
        finished = (tail.compacted(since_ms=item.injected_at_ms) is True
                    if compaction else True)
        if took and finished:
            pending.clear(item.repl_pid)
            events.done(item)
            log.info("reflection: session %s took up its resume%s", who,
                     " after the compaction finished" if compaction else "")
            return _DONE, item
        if took:
            _lost(item, "the session took up its resume, but no compact_boundary "
                        "followed the /compact, so the compaction did not finish",
                  alive=True, read_status=read_status, **kw)
            return _LOST, item
        if not _alive(item):
            moved = _await_successor(item, who, **kw)
            if moved is None:
                _lost(item, "the session's process went away after its resume was "
                            "typed and before it was taken up, and no new process "
                            "took over the session", alive=False, **kw)
                return _LOST, item
            item = moved
            tail.rebind(item.repl_pid)
            if item.reissues >= MAX_REISSUES:
                _lost(item, f"the session's worker was swapped again after "
                            f"{MAX_REISSUES} re-issues", alive=True,
                      read_status=read_status, **kw)
                return _LOST, item
            try:
                if compaction and not _command_survived(tail, item):
                    return _REARM, _reissue(item, tail, who,
                                            read_status=read_status, **kw)
                deliver(item.repl_pid, item.followup, enter=True, tail=tail,
                        read_status=read_status,
                        what="the resume again after a worker swap", **kw)
            except DeliveryError as exc:
                _lost(item, f"its worker was swapped and the new one would not "
                            f"take the text: {exc}", alive=True,
                      read_status=read_status, **kw)
                return _LOST, item
            item = replace(item, reissues=item.reissues + 1,
                           last_outcome="resume typed again after a worker swap")
            pending.record(item)
            continue
        if clock() >= deadline:
            _lost(item, f"the session did not take up its resume"
                        f"{' or finish the compaction' if compaction else ''} "
                        f"within {int(FOLLOW_THROUGH_S // 60)} minutes",
                  alive=True, read_status=read_status, **kw)
            return _LOST, item
        sleep(FOLLOW_POLL_S)


def _follow_session_id(item: pending.Pending, tail, who: str) -> pending.Pending:
    """``item`` under the session id its process writes now.

    A /clear gives the session a new id in the same process. The promise must
    carry it, because a worker swap or a restart looks the session up by id.
    """
    current = tail.session_id
    if not current or current == item.session_id:
        return item
    log.info("reflection: session %s now writes as %s (a /clear); its promise "
             "follows", who, current)
    item = replace(item, session_id=current)
    pending.record(item)
    return item


def _await_and_resume_inner(item: pending.Pending, tail, who,
                            read_status=None, **kw) -> None:
    sleep = kw.get("sleep", time.sleep)
    failures = 0
    while failures < MAX_DELIVERY_ROUNDS:
        if item.phase == pending.RESUME_SENT:
            verdict, item = _follow_through(item, tail, who,
                                            read_status=read_status, **kw)
            if verdict == _REARM:
                continue
            return
        # Spacing between *delivery* rounds only. A round that ended in a hold —
        # the session had not reached its command yet — re-arms straight away, as
        # it always did: that is the wait working, and it is unbounded by design.
        pause = _round_backoff(failures)
        if pause:
            log.info("reflection: holding %ss before the next delivery round for "
                     "session %s (%s failed so far)", pause, who, failures)
            sleep(pause)
        outcome = watcher.await_completion(item.repl_pid, tail=tail,
                                           injected_at_ms=item.injected_at_ms,
                                           text=item.text, label=who,
                                           read_status=read_status)
        if _superseded(item):
            log.info("reflection: a newer command replaced the one session %s "
                     "was waiting on; its resume is that command's to send",
                     who)
            return
        item = _follow_session_id(item, tail, who)
        if outcome == watcher.VANISHED:
            moved = _await_successor(item, who, **kw)
            if moved is None:
                _lost(item, "the session's process went away before its command "
                            "ran, and no new process took over the session",
                      alive=False, **kw)
                return
            item = moved
            tail.rebind(item.repl_pid)
            if _command_survived(tail, item):
                continue
            if item.reissues >= MAX_REISSUES:
                _lost(item, f"the session's worker was swapped again after "
                            f"{MAX_REISSUES} re-issues", alive=True,
                      read_status=read_status, **kw)
                return
            try:
                item = _reissue(item, tail, who, read_status=read_status, **kw)
            except DeliveryError as exc:
                failures += 1
                item = pending.held(item, f"could not type the command again "
                                          f"after a worker swap: {exc}")
                pending.record(item)
            continue
        if outcome == watcher.TIMED_OUT:
            # Re-ask before nudging. A round can expire in the same second the
            # command starts — measured, first drill: the round ended 377ms
            # before the `/compact` start line — and a pause request typed then
            # is queued *during* compaction and drained into the fresh context
            # alongside the resume, which is the one place it does harm.
            tail.poll()
            if not watcher.has_started(tail, item.text, item.injected_at_ms):
                item = _hold_and_nudge(item, tail=tail, read_status=read_status,
                                       **kw)
                continue
            log.info("reflection: session %s reached its command just as the "
                     "round expired; sending its resume rather than a pause "
                     "request", who)
            outcome = watcher.STARTED_OUTCOME
        # A session seen to have STARTED its command is by definition mid-turn,
        # and queuing is exactly what we want from it — so the free-to-receive
        # check applies only to the settled path it was written for.
        if outcome == watcher.SETTLED_OUTCOME and not _free_to_receive(
                item.repl_pid, tail, read_status=read_status):
            item = pending.held(item, "the session started a turn between "
                                      "settling and the resume going in")
            pending.record(item)
            continue

        if _superseded(item):
            return
        # The resume is worded now, not when the command was queued: what a
        # change did is known only once its dialog has been answered.
        item = _resume_text(item, tail, **kw)
        tail.watch(item.followup)

        # A copy from an earlier round can turn up after its verify gave up on
        # it: a paste queued behind `/compact` shows only when compaction ends.
        tail.poll()
        if tail.landed(item.followup, since_ms=item.injected_at_ms) is True:
            log.info("reflection: the resume for session %s arrived from an "
                     "earlier round; not typing it again", who)
            item = replace(item, phase=pending.RESUME_SENT)
            pending.mark_delivered(item.repl_pid, item.injected_at_ms)
            pending.record(item)
            continue

        # The promise moves to `resume_sent` before the resume is typed, so a
        # restart in the gap replays a follow-through, which never types the
        # resume again unless a worker swap lost it.
        item = replace(item, phase=pending.RESUME_SENT)
        pending.mark_delivered(item.repl_pid, item.injected_at_ms)
        pending.record(item)
        if any(c.kind == "cd" for c in item.changes):
            _reconnect(item.repl_pid, who, tail=tail, read_status=read_status, **kw)
        try:
            deliver(item.repl_pid, item.followup, enter=True, tail=tail,
                    read_status=read_status, what="the deferred resume", **kw)
        except DeliveryError as exc:
            # Put the promise back, keeping its original `injected_at_ms`: that
            # is the watcher's reference for "the session reacted after this
            # moment", and a fresh one would make the next wait sit out its full
            # cap waiting for a reaction that already happened.
            failures += 1
            item = pending.held(replace(item, phase=pending.AWAITING_COMMAND),
                                f"could not be delivered: {exc}")
            pending.record(item)
            log.error("reflection: could not deliver the deferred resume to "
                      "session %s; it is still pending: %s", who, exc)
            continue

        landed = _verify_landed(tail, item.followup,
                                sleep=kw.get("sleep", time.sleep))
        if landed is None:
            # No transcript to read: not proof of anything, and certainly not
            # licence to type the same resume in twice.
            pending.clear(item.repl_pid)
            return
        if landed is True:
            continue
        failures += 1
        item = pending.held(replace(item, phase=pending.AWAITING_COMMAND),
                            "delivered, but the session's transcript never "
                            "showed it arriving")
        pending.record(item)
        log.warning("reflection: the deferred resume for session %s was sent but "
                    "its transcript never showed it arriving; holding the promise "
                    "and waiting for the session again", who)

    _lost(item, f"its resume could not be delivered after {MAX_DELIVERY_ROUNDS} "
                f"rounds", alive=_alive(item), read_status=read_status, **kw)


def _reconnect(repl_pid: int, who: str, **kw) -> None:
    """Type :data:`RECONNECT_AFTER_MOVE`; a failure is logged and the resume still goes in."""
    try:
        deliver(repl_pid, RECONNECT_AFTER_MOVE, enter=True,
                what="the MCP reconnect after a move", **kw)
    except DeliveryError as exc:
        log.warning("reflection: could not reconnect awm for session %s after its "
                    "move: %s", who, exc)


def resume_watch(item: pending.Pending, *,
                 spawn: Callable[[Callable[[], None]], Any] = None,
                 **kw) -> None:
    """Arm the wait-then-resume for a promise, on either lane.

    There is deliberately no transport branch here. The waiting half reads the
    session's own record, and the delivering half re-detects the lane when the
    time comes — so a session that was in a pane when its command was injected
    and is a background job by the time it finishes is simply found again.

    ``spawn`` defaults to the module-level :func:`_default_spawn`, resolved at
    call time rather than bound at def time — so a caller (or a test) that
    replaces :data:`inject._default_spawn` actually redirects the watcher.
    """
    spawn = spawn or _default_spawn
    spawn(lambda: _await_and_resume(item, **kw))


def replay_pending() -> dict[str, Any]:
    """Re-arm the watchers for follow-ups this service promised and did not deliver.

    Called once on boot. The gateway drains and respawns its services routinely —
    a restart, a deploy, a crash-respawn — and each of those takes every in-flight
    watcher thread with it. Without this pass a session that compacted at the
    wrong moment is simply left idle forever, with the caller already told the
    resume was on its way and nothing anywhere logging that it never came.

    Which promises survive is :func:`pending.load_all`'s question, and it asks
    the process: alive, and started when the promise says it did. Reachability is
    deliberately *not* checked — the lane is re-detected at delivery time,
    potentially minutes from now, and dropping a promise because the session
    happened to be mid-re-host at boot would throw away the very resume this
    exists to save. Nor is age: a promise held for an hour because its session
    never came free is this mechanism working, and re-arming it is the point.
    """
    items = pending.load_all()
    resumed = 0
    for item in items:
        log.info("reflection: re-arming the deferred resume for session %s "
                 "(a command was injected before this service restarted)",
                 item.name or item.session_id)
        resume_watch(item)
        resumed += 1
    return {"pending": len(items), "resumed": resumed}


def describe_caller(caller_pid: Optional[int]) -> dict[str, Any]:
    """What reflection thinks the caller is — useful when a refusal is confusing."""
    lane = _resolve(caller_pid)
    common = {"session": lane.name or lane.session_id,
              "session_id": lane.session_id, "pid": lane.repl_pid,
              "hosting": lane.hosting}
    if isinstance(lane, session_target.TmuxLane):
        return {**common, "pane": lane.pane}
    return common
