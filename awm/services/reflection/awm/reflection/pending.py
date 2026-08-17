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

The file is keyed by REPL pid — one per session, because a session can only be
running one command at a time — and carries the ``procStart`` observed when the
command was injected. That is the fail-closed check on replay: pids are recycled,
and a session record can be rewritten by whatever inherits the number, so
matching the record alone would let a stale promise type into a stranger.
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Optional

log = logging.getLogger("awm.reflection.pending")

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


def held(pending: Pending, outcome: str) -> Pending:
    """The same promise, one round older, with ``outcome`` as its reason.

    ``injected_at_ms`` deliberately does not move: it is the watcher's reference
    for "the session reacted after this moment", and refreshing it would make the
    next wait sit out its cap waiting for a reaction that already happened.
    """
    return replace(pending, holds=pending.holds + 1, last_outcome=outcome)


def _path(repl_pid: int) -> Path:
    return dir_path() / f"{repl_pid}.json"


def record(pending: Pending) -> None:
    """Write ``pending`` down before its watcher starts.

    Best-effort by design: a service that cannot write its state directory should
    still inject the command the caller asked for, and degrade to the old
    thread-only behaviour rather than refuse. The whole point is to lose fewer
    resumes, not to add a way to lose the command too.
    """
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


def load_all(*, now_ms: Optional[int] = None, proc_start=None) -> list[Pending]:
    """Every promise still worth keeping, clearing the ones that are not.

    Worth keeping means the session is still there: its pid is live and started
    when the promise says it did. That is the fail-closed test — pids are
    recycled, and a promise replayed against whatever inherited the number would
    type into a stranger — and it is the only test. Age is logged, never acted
    on.

    Unreadable and malformed records are removed here rather than left to
    accumulate: nothing else ever sweeps this directory, so a record that
    survives one pass unexplained would survive forever.
    """
    if proc_start is None:
        from awm.reflection.session_target import _proc_start as proc_start
    now = now_ms if now_ms is not None else int(time.time() * 1000)
    out: list[Pending] = []
    try:
        entries = sorted(dir_path().glob("*.json"))
    except OSError:
        return out
    for path in entries:
        try:
            data = json.loads(path.read_text())
            item = Pending(
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
            )
        except (OSError, ValueError, KeyError, TypeError) as exc:
            log.warning("reflection: discarding unreadable pending record %s: %s",
                        path.name, exc)
            try:
                path.unlink()
            except OSError:
                pass
            continue
        live = proc_start(item.repl_pid)
        if live is None:
            log.info("reflection: session %s (pid %s) is gone; dropping the "
                     "resume it was owed", item.name or item.session_id,
                     item.repl_pid)
            clear(item.repl_pid)
            continue
        if item.proc_start and item.proc_start != live:
            log.warning("reflection: pid %s was recycled since its resume was "
                        "promised (started %s, now %s); dropping it rather than "
                        "injecting into whatever holds that pid now",
                        item.repl_pid, item.proc_start, live)
            clear(item.repl_pid)
            continue
        age_ms = now - item.injected_at_ms
        if age_ms > OLD_ENOUGH_TO_MENTION_MS:
            log.info("reflection: session %s has been owed a resume for %ss (%s "
                     "round(s) of holding; last: %s)",
                     item.name or item.session_id, age_ms // 1000, item.holds,
                     item.last_outcome or "still waiting")
        out.append(item)
    return out
