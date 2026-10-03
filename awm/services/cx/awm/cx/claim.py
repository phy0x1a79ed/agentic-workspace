"""Handing a warm session to a terminal.

A claim is one request carrying the caller's directory. The session is moved
there by typing `/cd` into it, which is what makes the pool directory-agnostic:
Claude Code reloads project settings, project MCP servers, project skills and
the destination's CLAUDE.md on that move, so a moved session is equivalent to
one launched there.

CAUTION: CLAUDE.md accumulates across moves. The destination's file is added and
the origin's is not removed, which is why a session is seeded where there is no
CLAUDE.md and moved exactly once, on its way to its user. A session that already
carries an origin directory is refused here in code, not merely by the predicate.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any

from awm import claudedaemon

from awm.cx import config, pool, sessions, trust

log = logging.getLogger("awm.cx.claim")

#: Cap on every beat between authenticating, pasting and pressing Enter. The
#: reflection service's own values are sized for a session that may be mid-turn;
#: a warm session is idle with an empty composer, which is the easy case. These
#: beats are the difference between a claim that sits in front of an attach and
#: one you notice.
SETTLE_S = 0.05

#: How long to wait for a move to land before giving up on it.
#:
#: Generous on purpose. Once `/cd` has been typed the session is committed, so
#: giving up early costs the warm session *and* still pays for the cold launch
#: that follows. On an idle box a move lands in about 200ms; on this one under a
#: load average of 25 it took 1.8s, and the load that makes a move slow makes a
#: cold launch slow too.
MOVE_TIMEOUT_S = 6.0
POLL_S = 0.02


async def claim(cwd: str) -> dict[str, Any]:
    """Move the newest warm session to `cwd` and return its id.

    An empty `session` is the ordinary answer when there is nothing warm, and
    the caller reads it as "launch cold". Nothing here raises at the caller.
    """
    want = os.path.realpath(cwd or os.getcwd())
    if not os.path.isdir(want):
        return _miss(f"{want} is not a directory")
    async with pool.LOCK:
        version = sessions.binary_version()
        warm = pool.warm(sessions.load(), version=version)
        if not warm:
            return _miss("no warm session")
        s = warm[0]
        if s.origin_cwd is not None:  # the predicate says so too; say it twice
            return _miss(f"{s.short} has already been moved once")
        if not trust.trusted(want):
            return _miss(f"{want} would ask the user to trust it first")
        started = time.monotonic()
        try:
            await asyncio.to_thread(_type_cd, s, want)
        except claudedaemon.DaemonError as exc:
            return _miss(f"{s.short} would not take the move: {exc}")
        typed = time.monotonic()
        if not await _arrived(s.short, want):
            await asyncio.to_thread(_abandon, s)
            return _miss(f"{s.short} did not reach {want} in time")
        settled = _recheck(s.short, want)
        if settled is None:
            return _miss(f"{s.short} was taken while we were moving it")
        log.info("cx: %s claimed %s in %.0fms (typing %.0fms, landing %.0fms)",
                 want, s.short, (time.monotonic() - started) * 1000,
                 (typed - started) * 1000, (time.monotonic() - typed) * 1000)
        return {"session": s.short, "cwd": want}


def _miss(reason: str) -> dict[str, Any]:
    log.info("cx: no session handed out — %s", reason)
    return {"session": None, "reason": reason}


def _type_cd(s: sessions.Session, want: str) -> None:
    """Type `/cd <dir>` into the session, over the daemon's PTY socket."""
    def beat(seconds: float) -> None:
        time.sleep(min(seconds, SETTLE_S))

    with claudedaemon.open_lane(_lane(s), sleep=beat) as conn:
        # Ctrl-U first. An attempt that pasted but never committed would leave
        # its text in the composer for this one to concatenate onto, and an
        # empty composer does not notice the key.
        conn.send_keys(b"\x15")
        conn.type_text(f"/cd {want}")
        beat(SETTLE_S)
        conn.press_enter()


def _abandon(s: sessions.Session) -> None:
    """Leave a session that would not move exactly as idle as we found it.

    Escape dismisses whatever dialog swallowed the move, and Ctrl-U clears the
    composer. Without this the next claim's first keystrokes answer the dialog
    this one left open, and it answers it with the default — so one directory
    the pool cannot move to would cost every claim after it, not just its own.
    """
    try:
        with claudedaemon.open_lane(_lane(s)) as conn:
            conn.send_keys(b"\x1b")
            conn.send_keys(b"\x15")
    except claudedaemon.DaemonError as exc:
        log.warning("cx: could not clear %s after a failed move: %s", s.short, exc)


def _lane(s: sessions.Session) -> claudedaemon.DaemonLane:
    """Address the session straight from its roster entry.

    Reflection resolves a *caller* by walking process ancestry, because its whole
    guarantee is that it acts on nobody else. This is the opposite problem: the
    pool holds the id of a session it created itself, so the roster's own socket
    and token are the address. The handshake still checks whose REPL answers,
    which is what stops a recycled socket path from being typed into.
    """
    if not s.pty_sock or not s.pty_auth or not s.repl_pid:
        raise claudedaemon.DaemonError(f"{s.short} has no reachable PTY")
    return claudedaemon.DaemonLane(
        sock=s.pty_sock, auth=s.pty_auth, session_id=s.session_id or "",
        repl_pid=s.repl_pid, name=s.name, cli_version=s.cli_version,
        dec_modes=s.dec_modes,
    )


async def _arrived(short: str, want: str) -> bool:
    """Wait for the session's own record to say it is where we sent it.

    The only positive evidence of execution available. A slash command runs on
    the session's own turn, so at the moment the frames are written there is by
    construction nothing yet to observe.
    """
    deadline = asyncio.get_running_loop().time() + MOVE_TIMEOUT_S
    while asyncio.get_running_loop().time() < deadline:
        for s in sessions.load():
            if s.short == short and s.cwd and os.path.realpath(s.cwd) == want:
                return True
        await asyncio.sleep(POLL_S)
    return False


def _recheck(short: str, want: str) -> sessions.Session | None:
    """The session as it stands now, or None if it is no longer ours to give.

    The move stamps an origin directory, which is what makes a claimed session
    invisible to the next claimer — so this cannot re-run `claimable`. What it
    checks is that nobody else arrived during the move: the name still carries
    the pool's prefix, nothing has been said to it, and it is still running.
    """
    for s in sessions.load():
        if s.short != short:
            continue
        if sessions.is_ours(s) and s.tokens == 0 and sessions.is_alive(s):
            return s
        return None
    return None
