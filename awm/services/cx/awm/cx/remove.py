"""Deleting a session the pool made and nobody took.

Removal has its own predicate rather than being whatever claiming rejected.
"Delete the extras" would delete the session somebody is attached to and typing
into — on the box this was written against there was one that qualified.

`sessions.removable` is that predicate. This module is the acting half: it
plans by default, and before each deletion it re-reads the session's own record
rather than trusting the snapshot the plan was built from. Between a tick and
its removals a session can be claimed, renamed or prompted, and the window is
exactly as long as the deletions take.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from awm.cx import config, sessions

log = logging.getLogger("awm.cx.remove")

REMOVE_TIMEOUT_S = 20.0


def plan(now: float | None = None) -> list[dict[str, Any]]:
    """Every session that may be deleted, with the reason it may be."""
    version = sessions.binary_version()
    return [
        {"session": s.short, "name": s.name, "why": _why(s, version, now)}
        for s in sessions.load()
        if sessions.removable(s, version=version, now=now)
    ]


def _why(s: sessions.Session, version: str | None, now: float | None) -> str:
    if not sessions.is_alive(s):
        return "the process is gone"
    if version is not None and s.cli_version != version:
        return f"seeded by {s.cli_version}, the binary is now {version}"
    return f"aged out at {sessions.age_s(s, now) / 60:.0f} minutes"


async def apply(now: float | None = None) -> list[dict[str, Any]]:
    """Carry out the plan, re-checking each session as it comes up."""
    done = []
    for item in plan(now):
        short = item["session"]
        if not _still_removable(short):
            log.info("cx: %s stopped being removable between the plan and the "
                     "removal — left alone", short)
            done.append({**item, "removed": False, "why": "claimed while we looked"})
            continue
        done.append({**item, "removed": await _rm(short)})
    return done


def _still_removable(short: str) -> bool:
    version = sessions.binary_version()
    return any(s.short == short and sessions.removable(s, version=version)
               for s in sessions.load())


async def _rm(short: str) -> bool:
    proc = await asyncio.create_subprocess_exec(
        config.claude_bin(), "rm", short,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, err = await asyncio.wait_for(proc.communicate(), timeout=REMOVE_TIMEOUT_S)
    except (TimeoutError, asyncio.TimeoutError):
        proc.kill()
        log.warning("cx: removing %s timed out", short)
        return False
    if proc.returncode != 0:
        log.warning("cx: removing %s failed: %s", short,
                    (err or b"").decode(errors="replace").strip()[:200])
        return False
    log.info("cx: removed %s", short)
    return True
