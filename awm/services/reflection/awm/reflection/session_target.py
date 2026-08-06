"""Resolve a calling agent's REPL pid to something reflection can inject into.

Reflection acts on the caller and on nobody else. That guarantee is only as good
as the identity it starts from, so this module is deliberately narrow: it takes a
process id that the gateway observed (never one the model supplied), and either
returns the one place that pid lives — or refuses.

The pid is trustworthy because of how the MCP proxy is spawned. Claude Code runs
``awm-mcp`` as a stdio child of the REPL, one proxy per session, so the proxy's
own parent *is* the calling agent. That holds identically whether the session is
hosted in a tmux pane or as a background job, which is the whole reason the
service no longer needs to care which it is.

From the pid, Claude Code's own per-session record at
``~/.claude/sessions/<repl-pid>.json`` says how the session is hosted:

* ``kind: "interactive"`` — a terminal session. Find its tmux pane by walking
  process ancestry; injection is tmux paste/send-keys.
* ``kind: "bg"`` — a background job under ``claude daemon``. Join the record's
  ``sessionId`` against the daemon roster for the worker's PTY socket and input
  token; injection speaks the daemon's socket protocol.

Anything else refuses. There is no fallback that picks a plausible session:
guessing here means typing into a stranger's prompt.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

log = logging.getLogger("awm.reflection.session_target")

# Overridable for tests; both are user-private (mode 0600) in practice.
SESSIONS_DIR = Path(os.path.expanduser("~/.claude/sessions"))
ROSTER_PATH = Path(os.path.expanduser("~/.claude/daemon/roster.json"))


class ResolveError(RuntimeError):
    """The caller could not be identified, so there is nothing safe to target."""


@dataclass(frozen=True)
class TmuxTarget:
    """An interactive session living in a tmux pane."""
    pane: str
    session_id: str
    repl_pid: int
    name: Optional[str] = None
    kind: str = "tmux"


@dataclass(frozen=True)
class DaemonTarget:
    """A background session whose PTY is hosted by the Claude Code daemon."""
    sock: str
    auth: str
    session_id: str
    repl_pid: int
    name: Optional[str] = None
    cli_version: Optional[str] = None
    dec_modes: tuple[int, ...] = ()
    kind: str = "daemon"


def _proc_start(pid: int) -> Optional[str]:
    """``/proc/<pid>/stat`` field 22 (starttime), or ``None`` if the pid is gone.

    Field 2 (comm) is parenthesised and may itself contain spaces and parens, so
    everything before the *last* ``)`` is skipped; after that, field N sits at
    index N-3.
    """
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as fp:
            data = fp.read()
    except OSError:
        return None
    try:
        return data[data.rfind(")") + 2:].split()[19]
    except IndexError:
        return None


def read_session_record(repl_pid: int) -> dict:
    """Return Claude Code's session record for ``repl_pid``.

    Raises :class:`ResolveError` if there is no record, it is unreadable, or it
    describes a *different* process that has since inherited this pid. Records
    are keyed by pid and pids get recycled, so the record's ``procStart`` is
    checked against the live process's start time before it is trusted — without
    that, a long-dead session's record could point injection at whatever now
    holds its number.
    """
    path = SESSIONS_DIR / f"{repl_pid}.json"
    try:
        record = json.loads(path.read_text())
    except FileNotFoundError:
        raise ResolveError(
            f"no Claude Code session record for pid {repl_pid}; the caller does "
            f"not look like a Claude Code session, so there is nothing to inject "
            f"into") from None
    except (OSError, ValueError) as exc:
        raise ResolveError(f"could not read session record for pid {repl_pid}: "
                           f"{exc}") from None

    live = _proc_start(repl_pid)
    if live is None:
        raise ResolveError(f"calling process {repl_pid} is gone")
    claimed = str(record.get("procStart", ""))
    if claimed and claimed != live:
        raise ResolveError(
            f"session record for pid {repl_pid} is stale (it describes a process "
            f"started at {claimed}, but pid {repl_pid} started at {live} — the pid "
            f"was recycled); refusing to inject")
    return record


def _roster_worker(session_id: str) -> dict:
    """Return the daemon roster entry whose ``sessionId`` is ``session_id``.

    Joined on ``sessionId`` and never on pid: the roster's ``pid`` is the
    ``bg-pty-host`` process, not the REPL. The roster's *key* is a short id that
    also does not reliably prefix the session id.
    """
    try:
        roster = json.loads(ROSTER_PATH.read_text())
    except FileNotFoundError:
        raise ResolveError(
            "the Claude Code daemon roster does not exist, so this background "
            "session's PTY cannot be located") from None
    except (OSError, ValueError) as exc:
        raise ResolveError(f"could not read the daemon roster: {exc}") from None

    for worker in (roster.get("workers") or {}).values():
        if worker.get("sessionId") == session_id:
            return worker
    raise ResolveError(
        f"no daemon roster entry for session {session_id}; the background "
        f"session's PTY host may have exited")


def resolve(repl_pid: int, *, socket: Optional[str] = None,
            runner=None) -> TmuxTarget | DaemonTarget:
    """Resolve a caller's REPL pid to its injection target.

    ``socket``/``runner`` are only consulted on the tmux path (they are the tmux
    socket override and the subprocess runner seam used by tests).
    """
    from awm.reflection import tmux_inject

    if not isinstance(repl_pid, int) or repl_pid <= 0:
        raise ResolveError(f"invalid caller pid {repl_pid!r}")

    record = read_session_record(repl_pid)
    kind = record.get("kind")
    session_id = record.get("sessionId") or ""
    name = record.get("name")

    if kind == "interactive":
        kw = {"socket": socket}
        if runner is not None:
            kw["runner"] = runner
        pane = tmux_inject.pane_for_pid(repl_pid, **kw)
        if pane is None:
            raise ResolveError(
                f"session {name or session_id} (pid {repl_pid}) reports itself as "
                f"an interactive terminal session, but no tmux pane on this server "
                f"contains it; reflection can only reach terminal sessions that "
                f"run under tmux")
        return TmuxTarget(pane=pane, session_id=session_id, repl_pid=repl_pid,
                          name=name)

    if kind == "bg":
        worker = _roster_worker(session_id)
        sock = worker.get("ptySock")
        auth = worker.get("ptyAuth")
        if not sock or not auth:
            raise ResolveError(
                f"the daemon roster entry for session {session_id} has no PTY "
                f"socket or input token; reflection cannot reach it")
        modes = worker.get("decModes") or []
        return DaemonTarget(
            sock=sock, auth=auth, session_id=session_id, repl_pid=repl_pid,
            name=name, cli_version=worker.get("cliVersion"),
            dec_modes=tuple(int(m) for m in modes if isinstance(m, int)),
        )

    raise ResolveError(
        f"session {name or session_id} (pid {repl_pid}) has an unrecognised "
        f"hosting kind {kind!r}; reflection knows how to reach interactive tmux "
        f"sessions and background daemon sessions")
