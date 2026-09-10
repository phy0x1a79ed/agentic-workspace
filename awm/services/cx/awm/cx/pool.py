"""The pool's read-only view of itself — what `cx status` answers with.

Everything here is derived from the live session records at the moment it is
asked. There is no stored pointer to the current warm session, because a stored
pointer is a second thing that has to agree with the records and it is the
half that goes stale. "The warm session" is whichever claimable one is newest.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from awm.cx import config, sessions

#: Serialises a claim against the removal half of a reconcile tick.
#: Both run in this one process and this one event loop, so an
#: in-process lock is the whole of the mutual exclusion. Two claims
#: arriving together take it in turn: the first moves the session,
#: which stamps its origin directory, and the second then finds
#: nothing claimable and answers with a miss.
LOCK = asyncio.Lock()


def warm(all_sessions: list[sessions.Session], *, version: str | None,
         now: float | None = None) -> list[sessions.Session]:
    """The claimable sessions, newest first — the newest is the one to hand out.

    Newest rather than oldest because rotation replaces before it removes, so
    at any moment the pool may hold an old session on its way out beside its
    replacement. Handing out the newest gives its taker the most time before
    the daemon retires it.
    """
    return sorted(
        (s for s in all_sessions if sessions.claimable(s, version=version, now=now)),
        key=lambda s: s.started_at_ms or 0,
        reverse=True,
    )


def why_not(s: sessions.Session, *, version: str | None,
            now: float | None = None) -> str | None:
    """Why this session cannot be handed out, in a word a person can act on."""
    if version is None:
        return "no claude binary"
    if not s.has_record:
        return "deleted"
    if not sessions.is_ours(s):
        return "renamed"
    if s.tokens != 0:
        return "prompted"
    if not sessions.is_alive(s):
        return "gone"
    if s.origin_cwd is not None:
        return "claimed"
    if not sessions.is_untouched(s):
        return "used"
    if s.cli_version != version:
        return f"binary {s.cli_version}"
    if sessions.age_s(s, now) >= config.rotate_age_s():
        return "aged out"
    return None


def status(loop: Any = None) -> dict[str, Any]:
    """The whole pool in one reply: what is held, and whether it can be held."""
    now = time.time()
    version = sessions.binary_version()
    pid = sessions.daemon_pid()
    # A session the pool seeded and somebody then adopted still appears here,
    # marked "renamed". Where a session went is the question status exists to
    # answer, and dropping it the instant it is renamed answers nothing.
    prefix = config.name_prefix()
    mine = [s for s in sessions.load()
            if s.has_record
            and (sessions.is_ours(s) or (s.seed_name or "").startswith(prefix))]
    rows = []
    for s in mine:
        rows.append({
            "session": s.short,
            "name": s.name,
            "age_s": round(sessions.age_s(s, now), 1),
            "alive": sessions.is_alive(s),
            "cwd": s.cwd,
            "cli_version": s.cli_version,
            "claimable": sessions.claimable(s, version=version, now=now),
            "why_not": why_not(s, version=version, now=now),
            "removable": sessions.removable(s, version=version, now=now),
        })
    rows.sort(key=lambda r: r["age_s"])
    out: dict[str, Any] = {
        "warm": [r["session"] for r in rows if r["claimable"]],
        "want": config.want(),
        "sessions": rows,
        "daemon_pid": pid,
        "seeding": "ok" if pid else "refused: no claude code daemon is running",
        "binary_version": version,
        "prefix": prefix,
        "rotate_age_s": config.rotate_age_s(),
    }
    out.update(loop.status() if loop is not None else
               {"loop": "not running", "holds_lock": False, "last_tick": None})
    return out
