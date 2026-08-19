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

The confirmation deliberately comes *after* the commit. It used to come before:
the text was read back off the lane and Enter was withheld unless it was visible.
That works on tmux, whose ``capture-pane`` renders current state, and does not
work at all on the daemon lane, which is a stream of the TUI's repaint deltas —
the TUI repaints the composer when it feels like it, and a paste it chose not to
paint was read as a paste that never arrived. Background sessions were written to
correctly and then never submitted. What can be checked on both lanes is what the
*session* did, and that only exists once Enter is on the wire.

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
``daemon_inject``'s module docstring.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from typing import Any, Callable, NamedTuple, Optional

from awm.reflection import (
    daemon_inject,
    guards,
    pending,
    session_target,
    stop_gate,
    tmux_inject,
    transcript,
    watcher,
)

log = logging.getLogger("awm.reflection.inject")

# Three tries, as agreed. Enough to cross a single re-host plus one unlucky beat;
# not so many that a genuinely unreachable session is ground against while the
# caller blocks on the verb.
ATTEMPTS = 3

# How much of the text to look for in the read-back. A short distinctive prefix
# beats the whole string: the TUI wraps long input across the prompt box and
# paints border glyphs at the wrap, which would defeat a full-line match. The
# first few characters land right after the prompt marker and cannot wrap.
_PROBE_CHARS = 16

# CSI / OSC escape sequences. The daemon lane hands back the raw pty stream with
# these still in it; tmux's `capture-pane` is already plain, and stripping a
# string with none in it is harmless.
_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b.")

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

# How many times a session will be asked to bring its turn to a close before the
# wait goes quiet and simply keeps holding. A pause request is text arriving
# unbidden in somebody's session, so it is rationed: three across the life of a
# promise, at most one per fifteen-minute round.
NUDGE_LIMIT = 3

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


class NotVerified(RuntimeError):
    """The text was written but did not show up in the lane's read-back.

    Only raised on a lane whose read-back renders current state, where "not on
    screen" means what it says. See ``read_back_is_evidence``.
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
            "come through a session's own awm-mcp proxy (a plain shell, for "
            "instance)")
    return session_target.resolve(caller_pid)


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------

def _flatten(screen: str) -> str:
    """Strip escapes and all whitespace, so wrapping cannot hide a match.

    Removing whitespace outright rather than collapsing it is deliberate: a wrap
    inside the probe would otherwise insert a newline that no amount of
    normalising a *space* would forgive.
    """
    return "".join(_ANSI.sub("", screen).split())


def _probe(text: str) -> str:
    return _flatten(text)[:_PROBE_CHARS]


def _landed(before: str, after: str, probe: str) -> bool:
    """Did ``probe`` appear *more* times after the write than before it?

    Presence alone is not evidence. Both lanes hand back text that still holds
    earlier paints — scrollback for tmux, an append-only byte stream for the
    daemon — so a session that compacted an hour ago still has ``/compact`` on
    screen, and a presence check would call the write verified without a single
    byte having landed. An increase in the count cannot be faked that way.

    A *false* here is only meaningful on a lane that renders. On the daemon lane
    it means "the TUI did not choose to repaint", which it frequently does not,
    and reading that as failure is what stopped background sessions compacting
    themselves. The caller checks ``read_back_is_evidence`` before acting on it.
    """
    if not probe:
        return True
    return _flatten(after).count(probe) > _flatten(before).count(probe)


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
        return daemon_inject.open_lane(
            lane, **{k: v for k, v in kw.items() if k in ("opener", "sleep")})
    return tmux_inject.open_lane(
        lane, **{k: v for k, v in kw.items()
                 if k in ("socket", "runner", "sleep")})


_LANE_FAILURES = (session_target.ResolveError, tmux_inject.TmuxError,
                  daemon_inject.DaemonError, NotVerified, NotSubmitted, OSError)


def _attempt(repl_pid: int, text: str, *, enter: bool, clear_first: bool,
             detect, read_status=None, tail=None, **kw) -> Delivery:
    """One detect → write → commit → confirm transaction. Raises on any failure.

    The confirmation is the last step, not the third. It used to sit between the
    write and the commit, gating Enter on the text being visible in the lane's
    read-back — which the daemon lane cannot answer, so background sessions were
    written to correctly and then never submitted. What the write can be checked
    against is what the *session* did with it, and that is only observable once
    Enter is on the wire.
    """
    lane = detect(repl_pid)
    # Sampled before anything is typed, because it is the reference the
    # confirmation compares against: "did this move?" needs a from.
    was = (read_status or watcher.read_status)(repl_pid)
    with _open_lane(lane, **kw) as writer:
        if clear_first:
            writer.clear()
        before = writer.read_back()
        writer.write(text)
        after = writer.read_back()
        if (not _landed(before, after, _probe(text))
                and writer.read_back_is_evidence):
            raise NotVerified(
                f"the text was written to {writer.label} but never showed up "
                f"there; the session is not reading its pty, or the paste was "
                f"swallowed by a modal")
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
    detect = detect or session_target.resolve
    sleep = kw.get("sleep", time.sleep)
    failures: list[str] = []
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
         followup: Optional[str] = None,
         spawn: Callable[[Callable[[], None]], Any] = _default_spawn,
         **kw) -> dict:
    """Inject ``text`` into the calling session, whatever is hosting it."""
    if not caller_pid:
        raise session_target.ResolveError(
            "could not tell which session is calling, so there is nothing safe "
            "to inject into. Reflection types into the caller's own prompt and "
            "will not guess at a target — this usually means the call did not "
            "come through a session's own awm-mcp proxy (a plain shell, for "
            "instance)")
    # Ask the guards before announcing or detecting anything. This used to run
    # after the log line, which made the log claim a refused `/clear` had gone
    # in — and on the one path where someone is trying to find out why a command
    # did not run, a log that overstates what happened is worse than no log.
    refused = guards.refusal(text, confirm=confirm)
    if refused is not None:
        return refused

    if delay_ms and delay_ms > 0:
        time.sleep(min(delay_ms, 25_000) / 1000.0)

    # Opened before a single keystroke goes out, so that whatever the session
    # does with the text is inside the window this reader can see.
    tail = transcript.Tail(caller_pid)
    tail.watch(text)
    tail.poll()

    injected_at_ms = watcher.now_ms()
    # A failure propagates: the caller is told the command did not go in, and —
    # because this sits above the record-and-spawn below — no promise is left
    # behind for a resume to a command that was never injected.
    result = deliver(caller_pid, text, enter=enter,
                     what="the requested command", tail=tail, **kw)
    submitted, lane = result.submitted, result.lane

    followup_sent = None
    followup_deferred = False
    if submitted and guards.is_slash(text):
        followup_sent = guards.resume_text(followup)
        followup_deferred = True
        # The queued command waits for this session's own turn to end, and the
        # turn-end-open-items Stop hook can block that exact Stop event if the
        # session is armed with open todos/background jobs. Disarm it before the
        # wait begins so reflection's own automation cannot stall behind a nag
        # it has no visibility into; `_await_and_resume` rearms it on every exit
        # path. If a crash lands between this and `pending.record` below, the
        # gate is left disarmed with no promise to later rearm it — accepted,
        # same as `pending.record`'s own best-effort framing.
        stop_gate.disarm(lane.session_id)
        promise = pending.Pending(
            repl_pid=lane.repl_pid,
            proc_start=session_target._proc_start(lane.repl_pid) or "",
            session_id=lane.session_id, text=text, followup=followup_sent,
            injected_at_ms=injected_at_ms, name=lane.name, hosting=lane.hosting)
        # Written down BEFORE the watcher starts: the watcher is a thread in this
        # process, and the gateway restarts this process out from under it.
        pending.record(promise)
        resume_watch(promise, spawn=spawn, tail=tail, **kw)

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
    if isinstance(lane, session_target.TmuxLane):
        out["pane"] = lane.pane
    return out


def _free_to_receive(repl_pid: int, tail) -> bool:
    """Is anything visibly wrong with typing into this session right now?

    The watcher has just said it settled, but a wait and a write are two
    different moments and a session can start a turn in the gap. This is the
    last-instant re-check, and it refuses only on what it can positively see —
    a `busy` or `waiting` status, or a foreground tool call outstanding. The
    watcher has already done the careful work; re-running its whole test here
    would throw away a fifteen-minute round over a record that happened to be
    mid-rewrite.
    """
    sample = watcher.read_status(repl_pid)
    if sample is not None and sample[0] in _NEVER_SETTLED:
        return False
    tail.poll()
    return tail.tool_call_in_flight() is not True


def _verify_landed(tail, text: str, *, sleep: Callable[[float], None] = time.sleep,
                   clock: Callable[[], float] = time.monotonic) -> Optional[bool]:
    """Did ``text`` reach the session? Read it out of the session's transcript.

    ``True`` if the session took it in or queued it, ``False`` if the transcript
    was readable throughout and never mentioned it, ``None`` if there was no
    transcript to read. Only ``False`` is evidence of a failure; ``None`` is the
    absence of evidence and must not license a second delivery.
    """
    deadline = clock() + VERIFY_WAIT_S
    while clock() < deadline:
        tail.poll()
        if tail.landed(text) is True:
            return True
        sleep(VERIFY_POLL_S)
    return tail.landed(text)


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


def _hold_and_nudge(item: pending.Pending, *, tail=None,
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
                what="a pause request", **kw)
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


def _await_and_resume(item: pending.Pending, *, tail=None, **kw) -> None:
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
    if tail is None:
        tail = transcript.Tail(item.repl_pid)
    tail.watch(item.text)
    tail.watch(item.followup)

    try:
        _await_and_resume_inner(item, tail, who, **kw)
    finally:
        # Rearms on every exit path below: VANISHED, delivered-and-verified, or
        # gave up after MAX_DELIVERY_ROUNDS. Covers replay_pending() for free,
        # since it re-enters this function per surviving promise.
        stop_gate.rearm(item.session_id)


def _await_and_resume_inner(item: pending.Pending, tail, who, **kw) -> None:
    failures = 0
    while failures < MAX_DELIVERY_ROUNDS:
        outcome = watcher.await_completion(item.repl_pid, tail=tail,
                                           injected_at_ms=item.injected_at_ms,
                                           text=item.text, label=who)
        if outcome == watcher.VANISHED:
            pending.clear(item.repl_pid)
            return
        if outcome == watcher.TIMED_OUT:
            # Re-ask before nudging. A round can expire in the same second the
            # command starts — measured, first drill: the round ended 377ms
            # before the `/compact` start line — and a pause request typed then
            # is queued *during* compaction and drained into the fresh context
            # alongside the resume, which is the one place it does harm.
            tail.poll()
            if not watcher.has_started(tail, item.text, item.injected_at_ms):
                item = _hold_and_nudge(item, tail=tail, **kw)
                continue
            log.info("reflection: session %s reached its command just as the "
                     "round expired; sending its resume rather than a pause "
                     "request", who)
            outcome = watcher.STARTED_OUTCOME
        # A session seen to have STARTED its command is by definition mid-turn,
        # and queuing is exactly what we want from it — so the free-to-receive
        # check applies only to the settled path it was written for.
        if outcome == watcher.SETTLED_OUTCOME and not _free_to_receive(
                item.repl_pid, tail):
            item = pending.held(item, "the session started a turn between "
                                      "settling and the resume going in")
            pending.record(item)
            continue

        # Forget the promise BEFORE delivering on it, not after. The two
        # orderings trade opposite failure modes across a restart in the gap:
        # clearing after would let a boot-time replay re-deliver a resume that
        # already landed, injecting a stray prompt into a session that is busy
        # working — whereas clearing first can only lose a resume if the process
        # dies inside the delivery itself, which is the same hole every other
        # step has.
        pending.clear(item.repl_pid)
        try:
            deliver(item.repl_pid, item.followup, enter=True, tail=tail,
                    what="the deferred resume", **kw)
        except DeliveryError as exc:
            # Put the promise back, keeping its original `injected_at_ms`: that
            # is the watcher's reference for "the session reacted after this
            # moment", and a fresh one would make the next wait sit out its full
            # cap waiting for a reaction that already happened.
            failures += 1
            item = pending.held(item, f"could not be delivered: {exc}")
            pending.record(item)
            log.error("reflection: could not deliver the deferred resume to "
                      "session %s; it is still pending: %s", who, exc)
            continue

        landed = _verify_landed(tail, item.followup,
                                sleep=kw.get("sleep", time.sleep))
        if landed is not False:
            # True, or "no transcript to read" — which is not proof of anything
            # and certainly not licence to type the same resume in twice.
            return
        failures += 1
        item = pending.held(item, "delivered, but the session's transcript never "
                                  "showed it arriving")
        pending.record(item)
        log.warning("reflection: the deferred resume for session %s was sent but "
                    "its transcript never showed it arriving; holding the promise "
                    "and waiting for the session again", who)

    log.error("reflection: gave up delivering the deferred resume to session %s "
              "after %s failed deliveries; the promise is left in `pending` and "
              "that session will sit idle until something resumes it",
              who, MAX_DELIVERY_ROUNDS)


def resume_watch(item: pending.Pending, *,
                 spawn: Callable[[Callable[[], None]], Any] = _default_spawn,
                 **kw) -> None:
    """Arm the wait-then-resume for a promise, on either lane.

    There is deliberately no transport branch here. The waiting half reads the
    session's own record, and the delivering half re-detects the lane when the
    time comes — so a session that was in a pane when its command was injected
    and is a background job by the time it finishes is simply found again.
    """
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
