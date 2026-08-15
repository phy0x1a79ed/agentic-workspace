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
detect the lane *fresh*, write the text, read the lane back to confirm the text
is actually on screen, and only then commit with Enter. Detection sits inside the
loop, immediately before the write, because the window between deciding an
address and using it is exactly where a session being attached or backgrounded
re-homes its pty out from under us — and that window should be as close to zero
as it can be made.

Enter is the commit point and the retry boundary. A failure *before* it means
nothing was submitted, so trying again is free; a failure *after* it may already
be running, and ``send`` carries arbitrary text, so nothing retries past it.
Attempts two and three clear the prompt first, which is what makes a retry
idempotent: a half-landed paste from the previous attempt is wiped rather than
concatenated onto. Attempt one does not clear, so an ordinary successful send
never destroys anything a human may have been mid-way through typing.

None of this establishes that the command *ran*. It establishes that the
keystrokes reached the TUI. The difference is structural and permanent — see
``daemon_inject``'s module docstring.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from typing import Any, Callable, Optional

from awm.reflection import (
    daemon_inject,
    guards,
    pending,
    session_target,
    tmux_inject,
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
    """The text was written but did not show up in the lane's read-back."""


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
    """
    if not probe:
        return True
    return _flatten(after).count(probe) > _flatten(before).count(probe)


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
                  daemon_inject.DaemonError, NotVerified, OSError)


def _attempt(repl_pid: int, text: str, *, enter: bool, clear_first: bool,
             detect, **kw) -> tuple[bool, Any]:
    """One detect → write → verify → commit transaction. Raises on any failure."""
    lane = detect(repl_pid)
    with _open_lane(lane, **kw) as writer:
        if clear_first:
            writer.clear()
        before = writer.read_back()
        writer.write(text)
        after = writer.read_back()
        if not _landed(before, after, _probe(text)):
            raise NotVerified(
                f"the text was written to {writer.label} but never showed up "
                f"there; the session is not reading its pty, or the paste was "
                f"swallowed by a modal")
        if not enter:
            return False, lane
        # Past this line nothing may be retried, so anything that goes wrong in
        # it is re-raised as the one error the retry loop does not catch.
        try:
            writer.commit()
        except Exception as exc:
            raise CommitFailed(
                f"the text reached {writer.label} and Enter was sent, but the "
                f"submit did not complete cleanly ({exc}); not retrying, "
                f"because it may already be running") from None
    return True, lane


def deliver(repl_pid: int, text: str, *, enter: bool = True,
            attempts: int = ATTEMPTS, detect=None, what: str = "text",
            **kw) -> tuple[bool, Any]:
    """Put ``text`` into session ``repl_pid``, or raise :class:`DeliveryError`.

    Returns ``(submitted, lane)`` — the lane being whichever one the write
    actually went to, which the caller needs for its result and its promise.

    Every attempt and every failure is logged. A send that only worked on the
    third try is the system telling you something about this host, and it should
    be readable later without anyone re-deriving it from timestamps.
    """
    detect = detect or session_target.resolve
    failures: list[str] = []
    for attempt in range(1, attempts + 1):
        try:
            submitted, lane = _attempt(repl_pid, text, enter=enter,
                                       clear_first=attempt > 1, detect=detect,
                                       **kw)
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
                        "via the %s lane (earlier attempts failed — %s)", what,
                        repl_pid, attempt, attempts, lane.kind,
                        "; ".join(failures))
        else:
            log.info("reflection: delivered %s to pid %s via the %s lane", what,
                     repl_pid, lane.kind)
        return submitted, lane
    log.error("reflection: giving up on delivering %s to pid %s after %s "
              "attempts — %s", what, repl_pid, attempts, "; ".join(failures))
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

    injected_at_ms = watcher.now_ms()
    # A failure propagates: the caller is told the command did not go in, and —
    # because this sits above the record-and-spawn below — no promise is left
    # behind for a resume to a command that was never injected.
    submitted, lane = deliver(caller_pid, text, enter=enter,
                              what="the requested command", **kw)

    followup_sent = None
    followup_deferred = False
    if submitted and guards.is_slash(text):
        followup_sent = guards.resume_text(followup)
        followup_deferred = True
        promise = pending.Pending(
            repl_pid=lane.repl_pid,
            proc_start=session_target._proc_start(lane.repl_pid) or "",
            session_id=lane.session_id, text=text, followup=followup_sent,
            injected_at_ms=injected_at_ms, name=lane.name, hosting=lane.hosting)
        # Written down BEFORE the watcher starts: the watcher is a thread in this
        # process, and the gateway restarts this process out from under it.
        pending.record(promise)
        resume_watch(promise, spawn=spawn, **kw)

    result = {"ok": True, "session": lane.name or lane.session_id,
              "hosting": lane.hosting, "text": text, "submitted": submitted,
              "followup": followup_sent, "followup_deferred": followup_deferred}
    if isinstance(lane, session_target.TmuxLane):
        result["pane"] = lane.pane
    return result


def _await_and_resume(item: pending.Pending, **kw) -> None:
    """Wait for the command to finish, then deliver the resume.

    Runs detached — a synchronous wait would deadlock, because the caller's turn
    has to end for the queued slash command to run at all.
    """
    who = item.name or item.session_id or f"pid {item.repl_pid}"
    outcome = watcher.await_completion(item.repl_pid,
                                       injected_at_ms=item.injected_at_ms,
                                       label=who)
    if outcome == watcher.VANISHED:
        pending.clear(item.repl_pid)
        return
    # Forget the promise BEFORE delivering on it, not after. The two orderings
    # trade opposite failure modes across a restart in the gap: clearing after
    # would let a boot-time replay re-deliver a resume that already landed,
    # injecting a stray prompt into a session that is busy working — whereas
    # clearing first can only lose a resume if the process dies inside the
    # delivery itself, which is the same hole every other step has.
    pending.clear(item.repl_pid)
    try:
        deliver(item.repl_pid, item.followup, enter=True,
                what="the deferred resume", **kw)
    except DeliveryError as exc:
        log.warning("reflection: could not deliver the deferred resume to "
                    "session %s: %s", who, exc)


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

    The one thing checked here is the stored ``procStart``: a pid outlives
    nothing, and a promise replayed against whatever inherited the number would
    type into a stranger. Reachability is deliberately *not* checked — the lane
    is re-detected at delivery time, potentially minutes from now, and dropping a
    promise because the session happened to be mid-re-host at boot would throw
    away the very resume this exists to save.
    """
    items = pending.load_all()
    resumed = 0
    for item in items:
        live = session_target._proc_start(item.repl_pid)
        if live is None:
            log.info("reflection: session %s (pid %s) is gone; dropping its "
                     "pending resume", item.name or item.session_id, item.repl_pid)
            pending.clear(item.repl_pid)
            continue
        if item.proc_start and item.proc_start != live:
            log.warning("reflection: pid %s was recycled since its resume was "
                        "promised (started %s, now %s); dropping it rather than "
                        "injecting into whatever holds that pid now",
                        item.repl_pid, item.proc_start, live)
            pending.clear(item.repl_pid)
            continue
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
