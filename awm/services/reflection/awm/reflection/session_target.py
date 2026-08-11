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
  ``jobId`` against the daemon roster for the worker's PTY socket and input
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

# Seam for tests: ``None`` means read the live process table. Anything else is
# called with no arguments and must return a ppid → [child pids] mapping.
PROCESS_CHILDREN = None


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


def _children() -> dict[int, list[int]]:
    """ppid → [child pids] for the live process table, via the test seam."""
    if PROCESS_CHILDREN is not None:
        return PROCESS_CHILDREN()
    from awm.reflection import tmux_inject
    return tmux_inject._ppid_children()


def _roster_worker(record: dict, repl_pid: int) -> dict:
    """Return the daemon roster entry hosting the background session ``record``.

    Keyed on ``jobId``, which names the worker directly and is fixed for the life
    of the job. ``sessionId`` is *not* stable: clearing a conversation replaces
    it in place, rewriting the record while the roster keeps the id the job was
    dispatched with — a session that did that could never be found again, and was
    told its PTY host had died. The ``sessionId`` scan survives only as a
    fallback for records written before ``jobId`` existed.

    Whatever matched is then checked against process ancestry, which is the claim
    that actually matters: the roster's ``pid`` is the ``bg-pty-host``, never the
    REPL, so the caller must sit somewhere in that host's subtree. A live host
    that does not contain the caller is a positive contradiction — the entry
    belongs to somebody else — and refusing there is what keeps a looser join key
    from ever widening who reflection can type into.
    """
    from awm.reflection import tmux_inject

    try:
        roster = json.loads(ROSTER_PATH.read_text())
    except FileNotFoundError:
        raise ResolveError(
            "the Claude Code daemon roster does not exist, so this background "
            "session's PTY cannot be located") from None
    except (OSError, ValueError) as exc:
        raise ResolveError(f"could not read the daemon roster: {exc}") from None

    workers = roster.get("workers") or {}
    job_id = str(record.get("jobId") or "")
    session_id = str(record.get("sessionId") or "")
    kids: Optional[dict[int, list[int]]] = None

    worker = workers.get(job_id) if job_id else None
    if worker is None:
        worker = next((w for w in workers.values()
                       if w.get("sessionId") == session_id and session_id), None)
    if worker is None:
        # Last resort, and last for a reason: the daemon supervisor is a common
        # ancestor of every background session, so this asks the narrow question
        # "which host contains me" rather than "which of my ancestors is listed".
        kids = _children()
        worker = next((w for w in workers.values()
                       if isinstance(w.get("pid"), int)
                       and tmux_inject._subtree_contains(w["pid"], repl_pid, kids)),
                      None)
    if worker is None:
        raise ResolveError(
            f"no daemon roster entry for job {job_id or '<unset>'} (session "
            f"{session_id or '<unset>'}), and no listed PTY host contains pid "
            f"{repl_pid}; the Claude Code daemon does not list this session")

    host = worker.get("pid")
    if isinstance(host, int) and _proc_start(host) is not None:
        if kids is None:
            kids = _children()
        if not tmux_inject._subtree_contains(host, repl_pid, kids):
            raise ResolveError(
                f"the daemon roster entry for job {job_id or '<unset>'} names PTY "
                f"host {host}, which is running but does not contain pid "
                f"{repl_pid}; that entry describes a different session, so "
                f"reflection will not inject into it")
    return worker


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
        worker = _roster_worker(record, repl_pid)
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
