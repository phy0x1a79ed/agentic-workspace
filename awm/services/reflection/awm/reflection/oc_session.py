"""Resolve an opencode caller's REPL pid to something reflection can inject into.

OpenCode has none of the files Claude Code writes — no per-pid session record,
no jsonl transcript. What it has instead is a SQLite DB
(``~/.local/share/opencode/opencode.db``) where every session carries the
directory it was opened in, and a ``opencode serve`` HTTP API that can both
create and receive messages for a session.

Identity is therefore the process's own cwd, observed, never model-supplied:

1. The process must *be* an opencode REPL (its ``/proc/<pid>/exe`` basename is
   ``opencode``). A recycled pid now holding something else refuses.
2. ``/proc/<pid>/cwd`` is the directory the session was opened in.
3. The DB session with that directory, no parent (a subagent's session carries
   a parent id and must never shadow the caller's own turn), and the most
   recent ``time_updated`` — that is the live conversation.

Two transports sit behind an :class:`OpencodeLane`, mirroring the two Claude
lanes:

* **tmux** — an interactive terminal session; ``pane_for_pid`` finds its pane.
* **background** — a session under ``opencode serve``; the serve URL is
  discovered by scanning the caller's fd table for its listening socket.

Anything else refuses. There is no fallback that picks a plausible session:
guessing here means typing into a stranger's prompt.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

log = logging.getLogger("awm.reflection.oc_session")

# Overridable for tests, like session_target.SESSIONS_DIR.
DB_PATH = Path(os.path.expanduser("~/.local/share/opencode/opencode.db"))


class ResolveError(RuntimeError):
    """The caller could not be identified as an opencode session."""


@dataclass(frozen=True)
class OpencodeLane:
    """An opencode session, reachable by tmux paste or serve HTTP."""
    session_id: str
    repl_pid: int
    name: Optional[str] = None
    pane: Optional[str] = None
    serve_url: Optional[str] = None
    kind: str = "opencode"
    hosting: str = "tmux"

    def __post_init__(self) -> None:
        if self.serve_url:
            object.__setattr__(self, "hosting", "background")


def _proc_start(pid: int) -> Optional[str]:
    """``/proc/<pid>/stat`` field 22 (starttime), or ``None`` if gone.

    The identity is observed live and the process is the caller, so a stale
    record cannot point at a recycled pid the way it can with Claude's
    pid-keyed files — but liveness is still asked, the same way the claude
    path asks it.
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


def _cwd(pid: int) -> Optional[str]:
    """``/proc/<pid>/cwd`` (the directory the session was opened in), or None."""
    try:
        return os.readlink(f"/proc/{pid}/cwd")
    except OSError:
        return None


def _is_opencode(pid: int) -> bool:
    """Is this process an opencode REPL, judged by its own exe name?

    The executable is the honest signal — the process *is* opencode. A wrapper
    that merely inherits the name is not in this path: the caller pid is the
    MCP proxy's parent, which opencode spawns directly (the wrapper problem the
    ancestry walk exists for is Claude's, and is handled in the gateway).
    """
    try:
        exe = os.readlink(f"/proc/{pid}/exe")
    except OSError:
        return False
    return os.path.basename(exe) == "opencode"


def serve_url_for(pid: int) -> Optional[str]:
    """Discover ``opencode serve``'s listening URL for ``pid``, or ``None``.

    The server binds ``127.0.0.1:<port>``; the port is read off its own fd
    table (``/proc/<pid>/fd`` → socket inode → ``/proc/net/tcp``). This is the
    direct analogue of the daemon lane's ``ptySock`` — an address to post the
    injection to, discovered from the caller's own process, not guessed.
    """
    import re
    try:
        fds = os.listdir(f"/proc/{pid}/fd")
    except OSError:
        return None
    sock_inodes = set()
    for fd in fds:
        try:
            link = os.readlink(f"/proc/{pid}/fd/{fd}")
        except OSError:
            continue
        m = re.fullmatch(r"socket:\[(\d+)\]", link)
        if m:
            sock_inodes.add(int(m.group(1)))
    if not sock_inodes:
        return None
    try:
        tcp = open("/proc/net/tcp", encoding="utf-8").read()
    except OSError:
        return None
    for line in tcp.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 10:
            continue
        try:
            inode = int(parts[9])
            local = parts[1]
        except (ValueError, IndexError):
            continue
        if inode in sock_inodes and parts[3] == "0A":
            port = int(local.split(":")[1], 16)
            return f"http://127.0.0.1:{port}"
    return None


def _resolve_session(directory: str):
    """The most recent parentless session opened in ``directory``, or ``None``.

    One deliberate query, not a scan: the identity is the caller's cwd, and the
    *current* conversation in that directory is the one most recently updated.
    Parentless, because a subagent's session (``parent_id`` set) is somebody
    else's turn and must never be mistaken for the caller's own.
    """
    import sqlite3
    try:
        conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
        try:
            rows = conn.execute(
                "SELECT id, slug, title FROM session "
                "WHERE directory = ? AND parent_id IS NULL "
                "ORDER BY time_updated DESC LIMIT 1",
                (directory,)).fetchall()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        log.warning("reflection: could not read the opencode session DB: %s", exc)
        return None
    return rows[0] if rows else None


def _default_pane_for_pid(pid: int) -> Optional[str]:
    from awm.reflection import tmux_inject
    return tmux_inject.pane_for_pid(pid)


def session_id_for(repl_pid: int) -> Optional[tuple[str, Optional[str]]]:
    """``(session_id, name)`` for the opencode session ``repl_pid`` owns, or None.

    The lighter half of :func:`resolve` — the identity is the caller's cwd and
    nothing more, so the transcript reader and the observation can find a
    session without deciding which transport reaches it. Returns ``None`` when
    the pid is not a live opencode session or has no parentless session open.
    """
    if _proc_start(repl_pid) is None or not _is_opencode(repl_pid):
        return None
    directory = _cwd(repl_pid)
    if not directory:
        return None
    row = _resolve_session(directory)
    if row is None:
        return None
    session_id, slug, title = row
    return session_id, title or slug


def resolve(repl_pid: int, *, expect_session: Optional[str] = None,
            pane_for_pid=None) -> OpencodeLane:
    """Resolve an opencode caller's pid to its injection target.

    ``expect_session`` narrows exactly as it does on the claude path: it can
    only *refuse*, and a mismatch raises so the caller's belief cannot widen
    who gets typed at.
    """
    if not isinstance(repl_pid, int) or repl_pid <= 0:
        raise ResolveError(f"invalid caller pid {repl_pid!r}")
    if _proc_start(repl_pid) is None:
        raise ResolveError(f"calling process {repl_pid} is gone")
    if not _is_opencode(repl_pid):
        raise ResolveError(
            f"pid {repl_pid} is not an opencode session (its executable is not "
            f"`opencode`), so the opencode adapter cannot reach it")

    directory = _cwd(repl_pid)
    if not directory:
        raise ResolveError(f"could not read the working directory of pid "
                           f"{repl_pid}; cannot locate its session")

    row = _resolve_session(directory)
    if row is None:
        raise ResolveError(
            f"no opencode session is open in {directory!r} (pid {repl_pid}); "
            f"the opencode database lists no parentless session there")
    session_id, slug, title = row
    name = title or slug

    if expect_session and session_id != expect_session:
        raise ResolveError(
            f"pid {repl_pid} resolves to opencode session {session_id}, but the "
            f"caller expected {expect_session}; that pid belongs to a different "
            f"conversation, so reflection will not act on it")

    pane_for_pid = pane_for_pid or _default_pane_for_pid
    pane = pane_for_pid(repl_pid)
    if pane is not None:
        return OpencodeLane(session_id=session_id, repl_pid=repl_pid,
                            name=name, pane=pane)

    serve_url = serve_url_for(repl_pid)
    if serve_url is not None:
        return OpencodeLane(session_id=session_id, repl_pid=repl_pid,
                            name=name, serve_url=serve_url)

    raise ResolveError(
        f"opencode session {name or session_id} (pid {repl_pid}) is neither "
        f"under a tmux pane nor served by an opencode process reflection can "
        f"dial; it cannot be reached")