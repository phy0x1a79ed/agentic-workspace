"""The daemon's own account of its starts and stops, read from daemon.log.

`claude daemon status` has no JSON mode and says nothing about the past, so
why the daemon last stopped, and for how long, comes from this log.
"""

from __future__ import annotations

import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from awm.claudedaemon import roster

HISTORY = 10

_START = re.compile(r"^\[(?P<at>[^\]]+)\] \[supervisor\] ─── daemon start ─── "
                    r"version=(?P<version>\S+) pid=(?P<pid>\d+) origin=(?P<origin>\S+)")
_STOP = re.compile(r"^\[(?P<at>[^\]]+)\] \[supervisor\] shutting down "
                   r"\(cause=(?P<cause>[^,)]+)(?:, uptime=(?P<uptime>\d+)s)?")


def path() -> Path:
    return roster.claude_home() / "daemon.log"


def history(text: str, limit: int = HISTORY) -> list[dict[str, Any]]:
    """Each daemon start in ``text`` (a daemon.log tail), with why the one
    before it stopped. A start with no shutdown line before it followed a
    SIGKILL, an OOM kill or a power loss: the daemon had no chance to log."""
    starts: list[dict[str, Any]] = []
    stop: dict[str, Any] | None = None
    for line in text.splitlines():
        if m := _STOP.match(line):
            stop = {"at": m["at"], "cause": m["cause"],
                         "uptime_s": int(m["uptime"]) if m["uptime"] else None}
        elif m := _START.match(line):
            after = stop or ({"cause": "killed", "at": starts[-1].get("at")} if starts else None)
            starts.append({"at": m["at"], "pid": int(m["pid"]), "version": m["version"],
                           "origin": m["origin"], "after": after,
                           "down_s": _between(after.get("at") if after else None, m["at"])})
            stop = None
    if stop:
        starts.append({"at": None, "pid": None, "version": None, "origin": None,
                       "after": stop})
    return starts[-limit:]


def _between(start: str | None, end: str | None) -> int | None:
    """Whole seconds between two daemon.log timestamps. For a kill the start is
    the previous start, so the figure is an upper bound."""
    try:
        a = datetime.fromisoformat(start.replace("Z", "+00:00"))  # type: ignore[union-attr]
        b = datetime.fromisoformat(end.replace("Z", "+00:00"))  # type: ignore[union-attr]
    except (AttributeError, ValueError):
        return None
    return max(0, round((b - a).total_seconds()))


def read_tail(log: Path | None = None, size: int = 256 * 1024) -> str:
    try:
        with open(log or path(), "rb") as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(0, fh.tell() - size))
            return fh.read().decode(errors="replace")
    except OSError:
        return ""


def last_stop(events: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Why the daemon most recently stopped, or None if the tail shows no stop."""
    return next((e["after"] for e in reversed(events) if e.get("after")), None)


def summary(pid: int | None, text: str | None = None) -> dict[str, Any]:
    """Up or down, since when, and why it last stopped, for one line of a reply.

    A daemon that is down while the log's last line is a start was killed
    without logging, so its cause is ``killed`` and its time is unknown.
    """
    events = history(read_tail() if text is None else text)
    latest = events[-1] if events else None
    stop = last_stop(events)
    if pid is not None:
        since = latest["at"] if latest and latest.get("pid") == pid else None
        return {"state": "up", "pid": pid, "since": since,
                "cause": stop.get("cause") if stop else None}
    if latest is not None and latest.get("pid") is not None:
        return {"state": "down", "pid": None, "since": None, "cause": "killed"}
    return {"state": "down", "pid": None, "since": stop.get("at") if stop else None,
            "cause": stop.get("cause") if stop else None}
