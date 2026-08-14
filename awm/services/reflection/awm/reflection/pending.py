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
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

log = logging.getLogger("awm.reflection.pending")

# Overridable for tests. Resolved lazily so importing this module never depends
# on a workspace being configured.
PENDING_DIR: Optional[Path] = None

# A promise older than this is not worth keeping: the watcher's own hard cap is
# 900s, so anything past it would have given up on its own long ago, and
# replaying it would resume a session that has since moved on by itself.
#
# Note what that assumes — that a watcher was alive to give up. It is false in
# the one case replay exists for: if this service stays down longer than
# MAX_AGE_MS, the boot sweep discards a promise nobody ever waited on and the
# session stays idle, which is the original bug narrowed to long outages.
# Raising the number is the wrong fix; asking the session's live status on
# replay, and delivering to one still sitting idle whatever its age, is the
# right one. Left open deliberately — restarts are seconds, not minutes.
MAX_AGE_MS = 20 * 60 * 1000


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


def load_all(*, now_ms: Optional[int] = None) -> list[Pending]:
    """Every promise still worth keeping, clearing the ones that are not.

    Unreadable, malformed, and stale records are removed here rather than left to
    accumulate: this directory is swept once per service boot and nothing else
    ever reads it, so a record that survives one sweep unexplained would survive
    forever.
    """
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
            )
        except (OSError, ValueError, KeyError, TypeError) as exc:
            log.warning("reflection: discarding unreadable pending record %s: %s",
                        path.name, exc)
            try:
                path.unlink()
            except OSError:
                pass
            continue
        if now - item.injected_at_ms > MAX_AGE_MS:
            log.info("reflection: pending resume for session %s is %ss old; "
                     "dropping it rather than resuming a session that has moved "
                     "on", item.name or item.session_id,
                     (now - item.injected_at_ms) // 1000)
            clear(item.repl_pid)
            continue
        out.append(item)
    return out
