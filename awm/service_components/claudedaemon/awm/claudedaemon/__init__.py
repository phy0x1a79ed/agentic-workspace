"""Typing into a background Claude Code session over the daemon's PTY socket.

Two services need this and neither owns it. `reflection` uses it to reach the
session that called it; `cx` uses it to move a warm session to the caller's
directory. It is pure wire protocol — it owns no state, supervises nothing, and
decides nothing about whose session may be reached, which stays with each
caller.

What stayed in `reflection` is the part that is that service's whole point:
resolving a caller to exactly one session and refusing anything it cannot
identify. See `awm.reflection.session_target`.
"""

from awm.claudedaemon.lane import DaemonLane
from awm.claudedaemon.pty import (
    Connection,
    DaemonError,
    SUPPORTED_PROTO,
    connect,
    frame,
    open_lane,
    open_unix,
)

__all__ = [
    "Connection",
    "DaemonError",
    "DaemonLane",
    "SUPPORTED_PROTO",
    "connect",
    "frame",
    "open_lane",
    "open_unix",
]
