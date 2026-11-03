"""On-disk record of a resume this service still owes a session.

The deferred follow-up is the whole reason a self-directed ``/compact`` is safe:
a bare slash command runs at end of turn and leaves the session idle with nothing
to do, so reflection waits for it to finish and then types a real prompt. That
wait is a detached watcher, and until this module existed it was *only* a thread
— which meant the promise lived exactly as long as the service process.

It does not. The gateway drains and respawns its services on every restart, and
the wait can run to fifteen minutes; a restart inside that window took the
watcher with it and the session sat idle forever. Nothing surfaced: the caller
had already been told ``followup_deferred: true`` and moved on, and the session
had no way to know a resume was ever coming. So the promise is written down
before the watcher starts, and :func:`load_all` is what the service reads on boot
to pick the waits back up.

A promise belongs to a session id. The file is named by the REPL pid, which is
only where that session lives now, and carries the ``procStart`` observed then.
Claude Code's daemon can swap a session's worker for a new process under the
same session id, so a dead pid sends the promise looking for its session under
a new pid (:func:`rekey`) before anything gives up on it. The ``procStart``
check stays fail-closed: pids are recycled, so a promise moves only to a live
process whose own record names its session id.

A promise is kept until the session has taken the resume up, not until the
resume was typed. ``phase`` says how far it got: ``awaiting_command`` until the
resume is typed, then ``resume_sent`` until the transcript shows it taken up
(and, for ``/compact``, the ``compact_boundary``). An enqueued line lives only
in the worker's memory, so typing it settles nothing.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Optional

from awm.cch import guards

log = logging.getLogger("awm.cch.pending")

# Overridable for tests. Resolved lazily so importing this module never depends
# on a workspace being configured.
PENDING_DIR: Optional[Path] = None

# Age is worth a log line and nothing more. A promise is kept for exactly as long
# as the session it belongs to is alive and still owed one, which is the question
# that actually matters — and it is asked of the process, not of the clock.
#
# There used to be a hard cutoff here, on the theory that a watcher past its own
# 900s cap had given up anyway. That theory assumed a watcher was alive to give
# up, which is false in the one case replay exists for: a service down longer
# than the cutoff came back and binned promises nobody had ever waited on. It is
# doubly wrong now that a resume is *held* rather than forced into a busy session
# — a long-held promise is the mechanism working, not a stale record.
OLD_ENOUGH_TO_MENTION_MS = 20 * 60 * 1000


def dir_path() -> Path:
    """Where pending records live."""
    if PENDING_DIR is not None:
        return PENDING_DIR
    from awm.config import SERVICES_DIR
    return SERVICES_DIR / "reflection" / "pending"


# What a settings change did. Empty until something finds out; a change that is
# still empty when its resume goes in is reported as having succeeded, because a
# session that settled without a confirm dialog open has nothing left to answer.
APPLIED = "applied"
FAILED = "failed"

_PHRASES = {
    "effort": "effort change to {value}",
    "model": "model change to {value}",
    "rename": "rename to {value}",
    "cd": "move to {value}",
}


AWAITING_COMMAND = "awaiting_command"
RESUME_SENT = "resume_sent"


@dataclass(frozen=True)
class Change:
    """One settings command a session ran on itself, and how it ended.

    ``key`` names the change across the merge of promises and the detached thread
    that answers its confirm dialog, so that thread can write the outcome back
    onto whichever promise now carries the change.
    """
    kind: str
    value: str = ""
    key: str = ""
    outcome: str = ""
    detail: str = ""


@dataclass(frozen=True)
class Pending:
    """A follow-up that has been promised but not yet delivered."""
    repl_pid: int
    proc_start: str
    session_id: str
    text: str
    followup: str
    injected_at_ms: int
    name: Optional[str] = None
    hosting: str = ""
    # Why this promise is still here. Written every time a round of waiting ends
    # without the resume landing, so that `reflection(verb="pending")` answers
    # "what is it waiting for" and not only "how long has it been".
    holds: int = 0
    last_outcome: str = ""
    # How many times the session has been asked to bring its turn to a close so
    # that the command it is holding can run. Counted on the promise rather than
    # in the watcher thread because the cap has to survive a restart: a session
    # that has already been asked three times should not be asked three more
    # every time this service is respawned.
    nudges: int = 0
    # The settings commands this promise reports on, and the caller's own closing
    # prompt (empty for the default). The delivered text is rendered from these at
    # delivery time, because a change's outcome is known only after its dialog has
    # been answered; ``followup`` is the same text rendered without outcomes.
    changes: tuple = ()
    closing: str = ""
    phase: str = AWAITING_COMMAND
    # How many times the command or the resume was typed again into a session
    # whose worker was swapped out from under it.
    reissues: int = 0
    # Names this promise in the events cch sends reflection (harness-contract.md).
    ticket: str = ""


def render_followup(changes, link: Optional[str] = None, closing: str = "") -> str:
    """The resume prompt for ``changes``: what each did, then the closing prompt.

    Without changes it is the closing prompt alone, or the default. A change
    with no outcome yet reads as succeeded.
    """
    tail = closing.strip() or guards.DEFAULT_FOLLOWUP
    done, failed, rc = [], [], False
    for c in changes:
        if c.kind == "rc_on":
            rc = True
            continue
        phrase = _PHRASES[c.kind].format(value=c.value)
        if c.outcome == FAILED:
            failed.append(f"The {phrase} did not apply ({c.detail or 'no reason given'}).")
        else:
            done.append(phrase)
    parts = []
    if done:
        parts.append("The " + " and the ".join(done) + " succeeded.")
    if rc:
        parts.append(f"Remote Control is on: {link}." if link else
                     "Remote Control was turned on; run reflection link to read "
                     "its link.")
    parts.extend(failed)
    return " ".join(parts + [tail])


def held(pending: Pending, outcome: str) -> Pending:
    """The same promise, one round older, with ``outcome`` as its reason.

    ``injected_at_ms`` deliberately does not move: it is the watcher's reference
    for "the session reacted after this moment", and refreshing it would make the
    next wait sit out its cap waiting for a reaction that already happened.
    """
    return replace(pending, holds=pending.holds + 1, last_outcome=outcome)


def nudged(pending: Pending) -> Pending:
    """The same promise, with one more pause request against it."""
    return replace(pending, nudges=pending.nudges + 1)


def _path(repl_pid: int) -> Path:
    return dir_path() / f"{repl_pid}.json"


_LOCK = threading.Lock()


def record(pending: Pending) -> None:
    """Write ``pending`` down before its watcher starts.

    An outcome already on disk for a change survives a rewrite from a watcher
    that has not seen it. Best-effort by design: a service that cannot write its
    state directory should still inject the command the caller asked for, and
    degrade to the old thread-only behaviour rather than refuse. The whole point
    is to lose fewer resumes, not to add a way to lose the command too.
    """
    with _LOCK:
        on_disk = read(pending.repl_pid)
        if on_disk is not None and pending.changes:
            known = {c.key: c for c in on_disk.changes if c.outcome}
            pending = replace(pending, changes=tuple(
                known[c.key] if not c.outcome and c.key in known else c
                for c in pending.changes))
        _write(pending)


def _write(pending: Pending) -> None:
    try:
        d = dir_path()
        d.mkdir(parents=True, exist_ok=True)
        tmp = d / f".{pending.repl_pid}.tmp"
        tmp.write_text(json.dumps(asdict(pending)))
        os.replace(tmp, _path(pending.repl_pid))
    except OSError as exc:
        log.warning("reflection: could not record the pending resume for pid %s "
                    "(%s); it will not survive a service restart",
                    pending.repl_pid, exc)


def clear(repl_pid: int) -> None:
    """Forget the promise for ``repl_pid``."""
    try:
        _path(repl_pid).unlink()
    except OSError:
        pass


def rekey(old_pid: int, item: Pending) -> None:
    """Move a promise from ``old_pid`` to the pid ``item`` now names."""
    with _LOCK:
        _write(item)
        if old_pid != item.repl_pid:
            clear(old_pid)


def _done_path(repl_pid: int) -> Path:
    return dir_path() / f"{repl_pid}.done"


def mark_delivered(repl_pid: int, injected_at_ms: int) -> None:
    """Note that the promise stamped ``injected_at_ms`` has gone in, or is going in.

    A watcher for an older promise that wakes after the newer one was delivered
    and cleared finds no file to tell it so; this stamp does.
    """
    with _LOCK:
        try:
            dir_path().mkdir(parents=True, exist_ok=True)
            _done_path(repl_pid).write_text(
                str(max(injected_at_ms, delivered_ms(repl_pid))))
        except OSError as exc:
            log.warning("reflection: could not note the delivery for pid %s: %s",
                        repl_pid, exc)


def delivered_ms(repl_pid: int) -> int:
    """The newest promise stamp delivered to ``repl_pid``, or 0."""
    try:
        return int(_done_path(repl_pid).read_text())
    except (OSError, ValueError):
        return 0


def _parse(data: dict) -> Pending:
    return Pending(
        repl_pid=int(data["repl_pid"]),
        proc_start=str(data["proc_start"]),
        session_id=str(data.get("session_id") or ""),
        text=str(data["text"]),
        followup=str(data["followup"]),
        injected_at_ms=int(data["injected_at_ms"]),
        name=data.get("name"),
        hosting=str(data.get("hosting") or ""),
        holds=int(data.get("holds") or 0),
        last_outcome=str(data.get("last_outcome") or ""),
        nudges=int(data.get("nudges") or 0),
        changes=tuple(
            Change(kind=str(c["kind"]), value=str(c.get("value") or ""),
                   key=str(c.get("key") or ""), outcome=str(c.get("outcome") or ""),
                   detail=str(c.get("detail") or ""))
            for c in data.get("changes") or ()),
        closing=str(data.get("closing") or ""),
        phase=str(data.get("phase") or AWAITING_COMMAND),
        reissues=int(data.get("reissues") or 0),
        ticket=str(data.get("ticket") or ""),
    )


def read(repl_pid: int) -> Optional[Pending]:
    """The promise on disk for ``repl_pid``, or None. No liveness check."""
    try:
        return _parse(json.loads(_path(repl_pid).read_text()))
    except (OSError, ValueError, KeyError, TypeError):
        return None


def has_change(repl_pid: int, key: str) -> bool:
    """Whether the promise on disk still reports the change ``key``."""
    found = read(repl_pid)
    return bool(found and any(c.key == key for c in found.changes))


def settle(repl_pid: int, key: str, outcome: str, detail: str = "") -> bool:
    """Write how change ``key`` ended onto the promise that carries it.

    False when no promise carries it, which is the normal case once the resume
    has gone in.
    """
    with _LOCK:
        found = read(repl_pid)
        if found is None or not any(c.key == key for c in found.changes):
            return False
        _write(replace(found, changes=tuple(
            replace(c, outcome=outcome, detail=detail) if c.key == key else c
            for c in found.changes)))
        return True


def _follow(item: Pending, proc_start) -> Optional[Pending]:
    """``item`` moved to the live process that holds its session now, or None.

    None means the session has no live process. The promise is then lost, and
    that is announced, never dropped in silence.
    """
    from awm.cch import events, session_target
    found = session_target.successor(item.session_id, item.repl_pid,
                                     proc_start=proc_start)
    if found is None:
        clear(item.repl_pid)
        events.lost(item, "the session has no live process any more, so "
                                "nothing is left to resume")
        return None
    pid, started = found
    log.warning("cch: session %s moved from pid %s to pid %s (a worker "
                "swap); its promise follows it", item.name or item.session_id,
                item.repl_pid, pid)
    moved = replace(item, repl_pid=pid, proc_start=started)
    rekey(item.repl_pid, moved)
    return moved


def load_all(*, now_ms: Optional[int] = None, proc_start=None) -> list[Pending]:
    """Every promise still worth keeping, clearing the ones that are not.

    Worth keeping means the session is still there: its pid is live and started
    when the promise says it did, or its session id lives on in a new process
    (:func:`_follow`). Pids are recycled, so a promise never stays on a pid whose
    start time changed. Age is logged, never acted on.

    Unreadable and malformed records are removed here rather than left to
    accumulate: nothing else ever sweeps this directory, so a record that
    survives one pass unexplained would survive forever.
    """
    if proc_start is None:
        from awm.cch.session_target import _proc_start as proc_start
    now = now_ms if now_ms is not None else int(time.time() * 1000)
    out: list[Pending] = []
    try:
        entries = sorted(dir_path().glob("*.json"))
        stamps = sorted(dir_path().glob("*.done"))
    except OSError:
        return out
    for stamp in stamps:
        if stamp.stem.isdigit() and proc_start(int(stamp.stem)) is None:
            try:
                stamp.unlink()
            except OSError:
                pass
    for path in entries:
        try:
            data = json.loads(path.read_text())
            item = _parse(data)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            log.warning("reflection: discarding unreadable pending record %s: %s",
                        path.name, exc)
            try:
                path.unlink()
            except OSError:
                pass
            continue
        live = proc_start(item.repl_pid)
        if live is None or (item.proc_start and item.proc_start != live):
            moved = _follow(item, proc_start)
            if moved is None:
                continue
            item = moved
        age_ms = now - item.injected_at_ms
        if age_ms > OLD_ENOUGH_TO_MENTION_MS:
            log.info("reflection: session %s has been owed a resume for %ss (%s "
                     "round(s) of holding, %s pause request(s); last: %s)",
                     item.name or item.session_id, age_ms // 1000, item.holds,
                     item.nudges, item.last_outcome or "still waiting")
        out.append(item)
    return out
