"""Live Claude session management — tracked, addressable, WS-streamable.

A "live session" is an `asyncio.subprocess.Process` running
``claude --print --input-format=stream-json --output-format=stream-json``
inside a scope's worktree. The process is registered in memory and persisted
to the ``agent_sessions`` table so callers can list/get/stop/kill by id.

A single WebSocket can attach at a time. Detaching does not kill the process.
On server restart, the in-memory registry is rebuilt as best-effort: live
processes are marked ``orphaned`` (manageable by id, but pipes are lost so
WS attach is refused).
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from awm.config import PROJECTS_DIR
from awm.db import get_connection
from awm.models import (
    AgentSessionCreateRequest,
    AgentSessionInfo,
)


_SUPPORTED_CLIS = {"claude"}  # only claude supports duplex stream-json
_BACKLOG_SIZE = 500           # ring buffer lines kept for WS attach backlog


class LiveSession:
    """In-memory handle for a running claude subprocess."""

    def __init__(
        self,
        id: int,
        project: str,
        scope: str,
        agent_cli: str,
        log_path: Path,
        proc: asyncio.subprocess.Process,
    ):
        self.id = id
        self.project = project
        self.scope = scope
        self.agent_cli = agent_cli
        self.log_path = log_path
        self.proc = proc
        self.status: str = "running"
        self.started_at: str = datetime.now(timezone.utc).isoformat()
        self.exited_at: Optional[str] = None
        self.exit_code: Optional[int] = None
        self.backlog: deque[str] = deque(maxlen=_BACKLOG_SIZE)
        self.ws = None  # only one WS attached at a time
        self.reader_task: Optional[asyncio.Task] = None
        self.waiter_task: Optional[asyncio.Task] = None

    @property
    def attached(self) -> bool:
        return self.ws is not None


_registry: dict[int, LiveSession] = {}
_registry_lock = asyncio.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _info_from_row(row) -> AgentSessionInfo:
    sess = _registry.get(row["id"])
    return AgentSessionInfo(
        id=row["id"],
        project=row["project"],
        scope=row["scope"],
        pid=row["pid"],
        status=row["status"],
        agent_cli=row["agent_cli"],
        started_at=row["started_at"],
        exited_at=row["exited_at"],
        exit_code=row["exit_code"],
        attached=bool(sess and sess.attached),
    )


# ---------------------------------------------------------------------------
# Create / lifecycle
# ---------------------------------------------------------------------------

async def create_session(req: AgentSessionCreateRequest) -> AgentSessionInfo:
    """Spawn a claude subprocess and register it.

    The claude binary is invoked with stream-json on stdin and stdout. If a
    prompt is given, it is written to stdin as a single user message line so
    the model starts working immediately even before any WS attaches.
    """
    cli = req.agent_cli or "claude"
    if cli not in _SUPPORTED_CLIS:
        raise ValueError(
            f"Unknown agent CLI '{cli}'. Live sessions require: {sorted(_SUPPORTED_CLIS)}"
        )

    workspace_dir = PROJECTS_DIR / req.project / req.scope
    if not workspace_dir.exists():
        raise FileNotFoundError(f"Scope workspace not found at {workspace_dir}")

    awm_dir = workspace_dir / ".awm"
    awm_dir.mkdir(parents=True, exist_ok=True)
    log_path = awm_dir / "session.log"

    # Insert "starting" row so we have an id before we spawn.
    started_at = _now()
    conn = get_connection()
    try:
        cur = conn.execute(
            "INSERT INTO agent_sessions "
            "(project, scope, pid, status, agent_cli, started_at, log_path) "
            "VALUES (?, ?, 0, 'starting', ?, ?, ?)",
            (req.project, req.scope, cli, started_at, str(log_path)),
        )
        session_id = cur.lastrowid
        conn.commit()
    finally:
        conn.close()

    # Spawn the subprocess. stderr goes to the per-scope log file.
    log_fp = open(log_path, "ab")
    try:
        proc = await asyncio.create_subprocess_exec(
            "claude",
            "--print",
            "--input-format=stream-json",
            "--output-format=stream-json",
            "--include-partial-messages",
            cwd=str(workspace_dir),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=log_fp,
            start_new_session=True,
        )
    except FileNotFoundError as exc:
        # claude binary missing — clean up the DB row
        log_fp.close()
        conn = get_connection()
        try:
            conn.execute(
                "UPDATE agent_sessions SET status='exited', exited_at=?, exit_code=-1 WHERE id=?",
                (_now(), session_id),
            )
            conn.commit()
        finally:
            conn.close()
        raise RuntimeError(f"claude binary not on PATH: {exc}") from exc
    finally:
        log_fp.close()  # file descriptor was duped into the child

    # Update DB with pid + running
    conn = get_connection()
    try:
        conn.execute(
            "UPDATE agent_sessions SET pid=?, status='running' WHERE id=?",
            (proc.pid, session_id),
        )
        conn.commit()
    finally:
        conn.close()

    session = LiveSession(
        id=session_id,
        project=req.project,
        scope=req.scope,
        agent_cli=cli,
        log_path=log_path,
        proc=proc,
    )
    session.started_at = started_at
    async with _registry_lock:
        _registry[session_id] = session

    # Inject initial prompt if provided. Format follows the stream-json input
    # contract (one JSON object per line, type=user).
    if req.prompt:
        try:
            await write_user_message(session, req.prompt)
        except Exception:
            pass  # don't fail the spawn if injection fails

    # Background pumps
    session.reader_task = asyncio.create_task(_reader_loop(session))
    session.waiter_task = asyncio.create_task(_waiter_loop(session))

    return _build_info(session)


def _build_info(session: LiveSession) -> AgentSessionInfo:
    return AgentSessionInfo(
        id=session.id,
        project=session.project,
        scope=session.scope,
        pid=session.proc.pid if session.proc else 0,
        status=session.status,
        agent_cli=session.agent_cli,
        started_at=session.started_at,
        exited_at=session.exited_at,
        exit_code=session.exit_code,
        attached=session.attached,
    )


async def write_user_message(session: LiveSession, text: str) -> None:
    """Write one stream-json user message line to claude's stdin."""
    if session.proc is None or session.proc.stdin is None:
        raise RuntimeError("session has no stdin (orphaned or exited)")
    if session.proc.stdin.is_closing():
        raise RuntimeError("session stdin already closed")
    payload = {
        "type": "user",
        "message": {"role": "user", "content": text},
    }
    line = (json.dumps(payload) + "\n").encode("utf-8")
    session.proc.stdin.write(line)
    await session.proc.stdin.drain()


# ---------------------------------------------------------------------------
# Background pumps
# ---------------------------------------------------------------------------

async def _reader_loop(session: LiveSession) -> None:
    """Read stdout lines, buffer them, forward to attached WS if any."""
    assert session.proc is not None and session.proc.stdout is not None
    stdout = session.proc.stdout
    while True:
        line = await stdout.readline()
        if not line:
            return  # EOF — process exited
        text = line.decode("utf-8", errors="replace").rstrip("\n")
        session.backlog.append(text)
        ws = session.ws
        if ws is not None:
            try:
                await ws.send_text(text)
            except Exception:
                # WS died mid-send; detach and keep buffering.
                session.ws = None


async def _waiter_loop(session: LiveSession) -> None:
    """Wait for the subprocess to exit, then update DB + status."""
    assert session.proc is not None
    exit_code = await session.proc.wait()
    session.exit_code = exit_code
    session.exited_at = _now()
    if session.status == "stopping":
        final = "exited"
    elif session.status == "killed":
        final = "killed"
    else:
        final = "exited"
    session.status = final

    conn = get_connection()
    try:
        conn.execute(
            "UPDATE agent_sessions SET status=?, exited_at=?, exit_code=? WHERE id=?",
            (final, session.exited_at, exit_code, session.id),
        )
        conn.commit()
    finally:
        conn.close()

    # If a WS is still attached, close it.
    ws = session.ws
    if ws is not None:
        try:
            await ws.close(code=1000, reason="session exited")
        except Exception:
            pass
        session.ws = None


# ---------------------------------------------------------------------------
# Query
# ---------------------------------------------------------------------------

def list_sessions(
    project: str | None = None,
    scope: str | None = None,
    status: str | None = None,
) -> list[AgentSessionInfo]:
    where = []
    params: list = []
    if project:
        where.append("project = ?")
        params.append(project)
    if scope:
        where.append("scope = ?")
        params.append(scope)
    if status:
        where.append("status = ?")
        params.append(status)
    sql = "SELECT * FROM agent_sessions"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id DESC"
    conn = get_connection()
    try:
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()
    return [_info_from_row(r) for r in rows]


def get_session(session_id: int) -> AgentSessionInfo | None:
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM agent_sessions WHERE id = ?", (session_id,)
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    return _info_from_row(row)


def tail_log(session_id: int, lines: int = 200) -> str:
    """Return the last N lines of the session's stderr log."""
    info = get_session(session_id)
    if info is None:
        raise FileNotFoundError(f"session {session_id} not found")
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT log_path FROM agent_sessions WHERE id = ?", (session_id,)
        ).fetchone()
    finally:
        conn.close()
    path = Path(row["log_path"])
    if not path.exists():
        return ""
    # Read whole file then tail — log files are small (per-session).
    with path.open("r", encoding="utf-8", errors="replace") as fp:
        data = fp.readlines()
    return "".join(data[-lines:])


# ---------------------------------------------------------------------------
# Stop / kill
# ---------------------------------------------------------------------------

async def stop_session(session_id: int) -> AgentSessionInfo:
    """Send SIGTERM. Mark status=stopping; waiter finalizes to exited."""
    session = _registry.get(session_id)
    info = get_session(session_id)
    if info is None:
        raise FileNotFoundError(f"session {session_id} not found")
    if info.status in ("exited", "killed"):
        return info

    if session is None:
        # Orphaned: we don't have the proc handle but we do have the pid.
        try:
            os.kill(info.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        conn = get_connection()
        try:
            conn.execute(
                "UPDATE agent_sessions SET status='stopping' WHERE id=?",
                (session_id,),
            )
            conn.commit()
        finally:
            conn.close()
        return get_session(session_id)

    session.status = "stopping"
    conn = get_connection()
    try:
        conn.execute(
            "UPDATE agent_sessions SET status='stopping' WHERE id=?", (session_id,)
        )
        conn.commit()
    finally:
        conn.close()
    try:
        session.proc.terminate()
    except ProcessLookupError:
        pass
    return _build_info(session)


async def kill_session(session_id: int) -> AgentSessionInfo:
    """SIGKILL. Mark status=killed."""
    session = _registry.get(session_id)
    info = get_session(session_id)
    if info is None:
        raise FileNotFoundError(f"session {session_id} not found")
    if info.status in ("exited", "killed"):
        return info

    if session is None:
        try:
            os.kill(info.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        conn = get_connection()
        try:
            conn.execute(
                "UPDATE agent_sessions SET status='killed', exited_at=? WHERE id=?",
                (_now(), session_id),
            )
            conn.commit()
        finally:
            conn.close()
        return get_session(session_id)

    session.status = "killed"
    conn = get_connection()
    try:
        conn.execute(
            "UPDATE agent_sessions SET status='killed' WHERE id=?", (session_id,)
        )
        conn.commit()
    finally:
        conn.close()
    try:
        session.proc.kill()
    except ProcessLookupError:
        pass
    return _build_info(session)


# ---------------------------------------------------------------------------
# WebSocket attach
# ---------------------------------------------------------------------------

class AttachError(Exception):
    """Raised when a WebSocket cannot attach (already attached or orphaned)."""


async def attach_ws(session_id: int, websocket) -> None:
    """Pump bytes between a WebSocket and the session's stdio.

    The WS-side contract is line-delimited stream-json: each WS text message
    is one JSON object that we forward verbatim to claude's stdin (appending a
    newline). Each stdout line from claude is sent back as one WS text frame.

    Backlog: when a WS attaches mid-run, the buffered stdout lines (up to
    _BACKLOG_SIZE) are replayed first.

    Detach: closing the WS does NOT terminate the subprocess. Stop/kill via
    the REST endpoints to end the session.
    """
    session = _registry.get(session_id)
    info = get_session(session_id)
    if info is None:
        raise FileNotFoundError(f"session {session_id} not found")
    if session is None:
        raise AttachError("session is orphaned (server restart lost its pipes)")
    if session.status not in ("running", "stopping"):
        raise AttachError(f"session is {session.status}")
    if session.ws is not None:
        raise AttachError("session already has an attached WebSocket")

    session.ws = websocket
    try:
        # Replay backlog
        for line in list(session.backlog):
            await websocket.send_text(line)

        # Pump WS → stdin
        while True:
            msg = await websocket.receive_text()
            if session.proc is None or session.proc.stdin is None:
                break
            if session.proc.stdin.is_closing():
                break
            # Assume client sends one stream-json object per message; add newline.
            data = msg if msg.endswith("\n") else msg + "\n"
            session.proc.stdin.write(data.encode("utf-8"))
            await session.proc.stdin.drain()
    finally:
        if session.ws is websocket:
            session.ws = None


# ---------------------------------------------------------------------------
# Reconciliation on startup
# ---------------------------------------------------------------------------

def reconcile_on_startup() -> None:
    """After a server restart, classify any non-terminal rows.

    We've lost the Popen handles, so we can't reattach pipes. For each row in
    (starting, running, stopping): if the pid is still alive, mark
    ``orphaned`` (controllable via stop/kill but not WS); else mark
    ``exited`` with no exit code.
    """
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT id, pid FROM agent_sessions "
            "WHERE status IN ('starting','running','stopping')"
        ).fetchall()
        for row in rows:
            pid = row["pid"]
            alive = False
            if pid > 0:
                try:
                    os.kill(pid, 0)
                    alive = True
                except (ProcessLookupError, PermissionError):
                    alive = False
            if alive:
                conn.execute(
                    "UPDATE agent_sessions SET status='orphaned' WHERE id=?",
                    (row["id"],),
                )
            else:
                conn.execute(
                    "UPDATE agent_sessions SET status='exited', exited_at=? "
                    "WHERE id=?",
                    (_now(), row["id"]),
                )
        conn.commit()
    finally:
        conn.close()
