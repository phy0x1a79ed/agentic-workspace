"""Everything awm knows about Claude Code's background sessions.

Submodules: `roster` reads the daemon roster and the per-job and per-process
records, `launch` starts one session, `trust` says whether a directory is
trusted, `sessionmode` says which restricted mode a session runs in, and `pty`,
`lane` and `job` type into a session over the daemon's PTY socket. The names
re-exported here are the PTY wire protocol.

It owns no state, supervises nothing, and decides nothing about whose session
may be reached or started, which stays with each caller. `cx`, `reflection`,
`transcripts`, the front door and the gateway import it.

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
