"""Where a background session's PTY lives, and what it takes to write to it."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class DaemonLane:
    """A background session whose PTY is hosted by the Claude Code daemon.

    This is the protocol's input, not a description of a session. Producing one
    means deciding that this caller may reach that session, which is the
    calling service's judgement to make and never this component's.
    """

    sock: str
    auth: str
    session_id: str
    repl_pid: int
    name: Optional[str] = None
    cli_version: Optional[str] = None
    dec_modes: tuple[int, ...] = ()
    kind: str = "daemon"
    hosting: str = "background"
