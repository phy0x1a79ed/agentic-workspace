"""Live Claude session management — rooms-aware, tracked, addressable.

A LiveSession owns one ``claude --input-format=stream-json
--output-format=stream-json`` subprocess for a single ``(project, scope)``.
Its job is the *agent runtime*: serialize inputs from any number of rooms
into stdin, parse stdout, and broadcast text/tool events to the rooms the
scope participates in.

Design changes from the prior single-WS model (M1):

- **No direct WS subscribers.** Subscribers attach to rooms (M4), not to
  sessions. ``attach_ws`` is gone.
- **Per-session input queue.** Posts from multiple rooms feed
  ``session.input_queue`` in FIFO order. ``_input_pump`` formats each as
  ``[room:<room_id> from:<author>]\\n<body>`` inside a stream-json user
  message and writes one line to stdin.
- **Per-line output broadcast.** ``_reader_loop`` parses each stdout line
  as stream-json (best-effort), extracts the user-visible body, and calls
  ``rooms.post(room_id, author='agent:<scope>', body=..., kind=...)``
  for every active room the scope participates in.
- **One process per scope.** Both an in-memory ``_by_scope`` map and a
  partial unique index on ``agent_sessions`` enforce uniqueness for
  active statuses.

The rooms service registers ``_local_scope_dispatcher`` against this
module so it can push posts into ``session.input_queue`` directly.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from awm.config import PROJECTS_DIR
from awm.db import get_connection
from awm.models import AgentSessionInfo
from awm.services import rooms as rooms_svc


_SUPPORTED_CLIS = {"claude"}
_INPUT_QUEUE_SIZE = 128

_ACTIVE_STATUSES = ("starting", "running", "stopping", "orphaned")


def _scope_key(project: str, scope: str) -> str:
    return f"{project}/{scope}"


# ---------------------------------------------------------------------------
# LiveSession
# ---------------------------------------------------------------------------

class LiveSession:
    """In-memory handle for a running claude subprocess.

    A session is identified by ``(project, scope)``; only one can be
    active at a time per scope.
    """

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
        self.scope_key = _scope_key(project, scope)
        self.agent_cli = agent_cli
        self.log_path = log_path
        self.proc = proc
        self.status: str = "running"
        self.started_at: str = _now()
        self.exited_at: Optional[str] = None
        self.exit_code: Optional[int] = None
        self.input_queue: asyncio.Queue[tuple[str, rooms_svc.Post]] = (
            asyncio.Queue(maxsize=_INPUT_QUEUE_SIZE)
        )
        self.reader_task: Optional[asyncio.Task] = None
        self.waiter_task: Optional[asyncio.Task] = None
        self.input_pump_task: Optional[asyncio.Task] = None
        # Stdin frames audit-log for tests (mirror of what gets written).
        self.stdin_frames_log = log_path.parent / "agent.log"


_registry: dict[int, LiveSession] = {}
_by_scope: dict[str, LiveSession] = {}
_registry_lock = asyncio.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ScopeBusyError(Exception):
    """A second LiveSession spawn for an already-running scope was attempted."""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def create_session(*, project: str, scope: str,
                         agent_cli: str = "claude") -> LiveSession:
    """Spawn a claude subprocess and register it. Raises ScopeBusyError
    if the scope already has an active session."""
    if agent_cli not in _SUPPORTED_CLIS:
        raise ValueError(
            f"Unknown agent CLI '{agent_cli}'. Live sessions require: {sorted(_SUPPORTED_CLIS)}"
        )

    key = _scope_key(project, scope)
    async with _registry_lock:
        if key in _by_scope:
            raise ScopeBusyError(
                f"scope {key} already has an active session "
                f"(id={_by_scope[key].id}, pid={_by_scope[key].proc.pid})"
            )

        workspace_dir = PROJECTS_DIR / project / scope
        if not workspace_dir.exists():
            raise FileNotFoundError(f"Scope workspace not found at {workspace_dir}")
        awm_dir = workspace_dir / ".awm"
        awm_dir.mkdir(parents=True, exist_ok=True)
        log_path = awm_dir / "session.log"

        started_at = _now()
        conn = get_connection()
        try:
            try:
                cur = conn.execute(
                    "INSERT INTO agent_sessions "
                    "(project, scope, pid, status, agent_cli, started_at, log_path) "
                    "VALUES (?, ?, 0, 'starting', ?, ?, ?)",
                    (project, scope, agent_cli, started_at, str(log_path)),
                )
                session_id = cur.lastrowid
                conn.commit()
            except Exception as exc:
                # Partial unique index violation surfaces here.
                raise ScopeBusyError(
                    f"DB rejected new session for {key}: {exc}"
                ) from exc
        finally:
            conn.close()

        # Spawn the subprocess.
        log_fp = open(log_path, "ab")
        try:
            proc = await asyncio.create_subprocess_exec(
                "claude",
                "--print",
                "--verbose",
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
            log_fp.close()

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
            id=session_id, project=project, scope=scope,
            agent_cli=agent_cli, log_path=log_path, proc=proc,
        )
        session.started_at = started_at
        _registry[session_id] = session
        _by_scope[key] = session

    # Background pumps (outside the lock).
    session.reader_task = asyncio.create_task(_reader_loop(session))
    session.waiter_task = asyncio.create_task(_waiter_loop(session))
    session.input_pump_task = asyncio.create_task(_input_pump(session))
    return session


def get_session_by_scope(project: str, scope: str) -> LiveSession | None:
    return _by_scope.get(_scope_key(project, scope))


def get_session(session_id: int) -> LiveSession | None:
    return _registry.get(session_id)


# ---------------------------------------------------------------------------
# Input pump — serialize per-room posts into stdin
# ---------------------------------------------------------------------------

async def _input_pump(session: LiveSession) -> None:
    """Drain ``input_queue`` and write each frame to stdin as one stream-json
    user message with ``[room:X from:Y]`` framing."""
    while True:
        try:
            room_id, post = await session.input_queue.get()
        except asyncio.CancelledError:
            return
        if session.proc is None or session.proc.stdin is None:
            return
        if session.proc.stdin.is_closing():
            return
        framed_body = f"[room:{room_id} from:{post.author}]\n{post.body}"
        payload = {
            "type": "user",
            "message": {"role": "user", "content": framed_body},
        }
        line = (json.dumps(payload) + "\n").encode("utf-8")
        try:
            session.proc.stdin.write(line)
            await session.proc.stdin.drain()
        except (ConnectionResetError, BrokenPipeError):
            return
        # Audit log for tests.
        try:
            with session.stdin_frames_log.open("a", encoding="utf-8") as fp:
                fp.write(f"STDIN {framed_body!r}\n")
        except OSError:
            pass


def enqueue_input(session: LiveSession, room_id: str, post: rooms_svc.Post) -> bool:
    """Non-blocking push onto the session's input queue. Returns False
    if the queue is full (caller may decide to drop or warn)."""
    try:
        session.input_queue.put_nowait((room_id, post))
        return True
    except asyncio.QueueFull:
        return False


# ---------------------------------------------------------------------------
# Output reader — parse stream-json, broadcast to rooms
# ---------------------------------------------------------------------------

def _extract_renderable(parsed: dict) -> list[tuple[str, str]]:
    """Return [(kind, body)] pairs to post for a parsed stream-json event.

    Stream-json shapes we care about:
      assistant: {message:{role,content:[{type:text,text:...},
                                          {type:tool_use,name:...,id:...,input:{}}]}}
      user (tool_result echo): {message:{content:[{type:tool_result,
                                                    tool_use_id,content}]}}
      result: {subtype, result, is_error}
      system, partial-message, etc: skipped
    """
    out: list[tuple[str, str]] = []
    t = parsed.get("type")
    if t == "assistant":
        content = parsed.get("message", {}).get("content", [])
        for block in content if isinstance(content, list) else []:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text":
                text = block.get("text", "").strip()
                if text:
                    out.append(("text", text))
            elif btype == "tool_use":
                name = block.get("name", "?")
                out.append(("tool_use", f"[tool_use: {name}]"))
    elif t == "user":
        content = parsed.get("message", {}).get("content", [])
        for block in content if isinstance(content, list) else []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_result":
                body_field = block.get("content", "")
                if isinstance(body_field, list):
                    body_field = " ".join(
                        c.get("text", "") for c in body_field if isinstance(c, dict)
                    )
                snippet = str(body_field).strip()
                if len(snippet) > 240:
                    snippet = snippet[:240] + "…"
                out.append(("tool_result", snippet or "[tool_result: empty]"))
    elif t == "result":
        body = parsed.get("result")
        if isinstance(body, str) and body.strip():
            out.append(("text", body.strip()))
    return out


def _rooms_for_scope(scope_key: str) -> list[str]:
    """Active locally-hosted rooms that include ``scope_key`` as a scope
    participant. Used to broadcast agent output."""
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT DISTINCT rp.room_id FROM room_participants rp "
            "JOIN rooms r ON r.id = rp.room_id "
            "WHERE rp.kind = 'scope' AND rp.identifier = ? "
            "AND rp.left_at IS NULL AND r.status = 'active'",
            (scope_key,),
        ).fetchall()
    finally:
        conn.close()
    return [r["room_id"] for r in rows]


async def _reader_loop(session: LiveSession) -> None:
    """Read stdout lines, parse, post to all participating rooms."""
    assert session.proc is not None and session.proc.stdout is not None
    stdout = session.proc.stdout
    while True:
        line = await stdout.readline()
        if not line:
            return
        text = line.decode("utf-8", errors="replace").rstrip("\n")
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            # Not stream-json — log only.
            try:
                with session.stdin_frames_log.open("a", encoding="utf-8") as fp:
                    fp.write(f"STDOUT(raw) {text}\n")
            except OSError:
                pass
            continue

        events = _extract_renderable(parsed)
        if not events:
            continue

        rooms = _rooms_for_scope(session.scope_key)
        if not rooms:
            continue
        author = f"agent:{session.scope_key}"
        for kind, body in events:
            for room_id in rooms:
                try:
                    rooms_svc.post(
                        room_id, author=author, body=body, kind=kind,
                    )
                except rooms_svc.RoomError:
                    continue


# ---------------------------------------------------------------------------
# Waiter — finalize on exit
# ---------------------------------------------------------------------------

async def _waiter_loop(session: LiveSession) -> None:
    assert session.proc is not None
    exit_code = await session.proc.wait()
    session.exit_code = exit_code
    session.exited_at = _now()
    final = "killed" if session.status == "killed" else "exited"
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

    # Cancel the input pump so it doesn't block waiting on a queue we'll
    # never service again.
    if session.input_pump_task is not None:
        session.input_pump_task.cancel()

    async with _registry_lock:
        if _by_scope.get(session.scope_key) is session:
            _by_scope.pop(session.scope_key, None)

    # Auto-close any rooms flagged close_on_exit that have no other agents.
    try:
        rooms_svc.auto_close_for_scope(session.scope_key)
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# Stop / kill
# ---------------------------------------------------------------------------

async def stop_session(session_id: int) -> AgentSessionInfo:
    session = _registry.get(session_id)
    info_row = _row_for(session_id)
    if info_row is None:
        raise FileNotFoundError(f"session {session_id} not found")
    if info_row["status"] in ("exited", "killed"):
        return _info_from_row(info_row)
    if session is None:
        # Orphan — signal via pid.
        try:
            os.kill(info_row["pid"], signal.SIGTERM)
        except ProcessLookupError:
            pass
        _set_status(session_id, "stopping")
        return _info_from_row(_row_for(session_id))

    session.status = "stopping"
    _set_status(session_id, "stopping")
    try:
        session.proc.terminate()
    except ProcessLookupError:
        pass
    return _info_from_row(_row_for(session_id))


async def kill_session(session_id: int) -> AgentSessionInfo:
    session = _registry.get(session_id)
    info_row = _row_for(session_id)
    if info_row is None:
        raise FileNotFoundError(f"session {session_id} not found")
    if info_row["status"] in ("exited", "killed"):
        return _info_from_row(info_row)
    if session is None:
        try:
            os.kill(info_row["pid"], signal.SIGKILL)
        except ProcessLookupError:
            pass
        _set_status(session_id, "killed", terminal=True)
        return _info_from_row(_row_for(session_id))

    session.status = "killed"
    _set_status(session_id, "killed")
    try:
        session.proc.kill()
    except ProcessLookupError:
        pass
    return _info_from_row(_row_for(session_id))


# ---------------------------------------------------------------------------
# Query helpers
# ---------------------------------------------------------------------------

def _row_for(session_id: int):
    conn = get_connection()
    try:
        return conn.execute(
            "SELECT * FROM agent_sessions WHERE id = ?", (session_id,),
        ).fetchone()
    finally:
        conn.close()


def _set_status(session_id: int, status: str, terminal: bool = False) -> None:
    now = _now()
    conn = get_connection()
    try:
        if terminal:
            conn.execute(
                "UPDATE agent_sessions SET status=?, exited_at=? WHERE id=?",
                (status, now, session_id),
            )
        else:
            conn.execute(
                "UPDATE agent_sessions SET status=? WHERE id=?",
                (status, session_id),
            )
        conn.commit()
    finally:
        conn.close()


def _info_from_row(row) -> AgentSessionInfo:
    return AgentSessionInfo(
        id=row["id"], project=row["project"], scope=row["scope"],
        pid=row["pid"], status=row["status"], agent_cli=row["agent_cli"],
        started_at=row["started_at"], exited_at=row["exited_at"],
        exit_code=row["exit_code"],
        attached=False,
    )


def list_sessions(project: str | None = None, scope: str | None = None,
                  status: str | None = None) -> list[AgentSessionInfo]:
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


def tail_log(session_id: int, lines: int = 200) -> str:
    row = _row_for(session_id)
    if row is None:
        raise FileNotFoundError(f"session {session_id} not found")
    path = Path(row["log_path"])
    if not path.exists():
        return ""
    with path.open("r", encoding="utf-8", errors="replace") as fp:
        data = fp.readlines()
    return "".join(data[-lines:])


# ---------------------------------------------------------------------------
# Reconciliation on startup
# ---------------------------------------------------------------------------

def reconcile_on_startup() -> None:
    """After a server restart, classify any non-terminal rows.

    Pids that are still alive become ``orphaned``; dead ones are
    flipped to ``exited``. ``_by_scope`` is repopulated with stub
    entries (proc=None) for orphans so spawn attempts for the same
    scope are correctly rejected — manage them via stop/kill.
    """
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT id, project, scope, pid FROM agent_sessions "
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


# ---------------------------------------------------------------------------
# Rooms-service dispatcher wiring
# ---------------------------------------------------------------------------

def _dispatch_local_post(room_id: str, scope_key: str,
                         post: rooms_svc.Post) -> None:
    """Called by rooms.post() when a local scope participant should receive
    an input frame. Drops the frame if the scope isn't running locally."""
    session = _by_scope.get(scope_key)
    if session is None:
        return
    enqueue_input(session, room_id, post)


def _on_close_room_kill(room_id: str) -> None:
    """Called when ``rooms.close_room(..., kill_agents=True)``. SIGTERM
    every scope participant that's running locally."""
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT identifier FROM room_participants "
            "WHERE room_id = ? AND kind = 'scope' AND left_at IS NULL",
            (room_id,),
        ).fetchall()
    finally:
        conn.close()
    for r in rows:
        ident = r["identifier"]
        if "@" in ident:
            continue  # remote scope — left to forward_agent_stop in M3
        session = _by_scope.get(ident)
        if session is None:
            continue
        try:
            session.proc.terminate()
        except ProcessLookupError:
            pass
        session.status = "stopping"
        _set_status(session.id, "stopping")


def _dispatch_remote_scope(peer_id: str, room_id: str, scope_key: str,
                           post: rooms_svc.Post) -> None:
    """Forward an input frame to a peer hosting the agent process."""
    from awm.services.network import federation
    try:
        federation.forward_agent_input(
            peer_id, room_id, scope_key,
            body=post.body, author=post.author,
        )
    except federation.FederationError:
        pass


def _dispatch_shadow_peer(peer_id: str, room_id: str,
                          post: rooms_svc.Post) -> None:
    """Push a locally-hosted room's post out to a subscribing peer."""
    from awm.services.network import federation
    try:
        federation.forward_room_post(
            peer_id, room_id, body=post.body, kind=post.kind,
            author=post.author,
        )
    except federation.FederationError:
        pass


def install_room_dispatchers() -> None:
    """Wire the rooms service to push input frames into LiveSessions and
    to kill agents on close_room(..., kill_agents=True)."""
    rooms_svc.set_local_scope_dispatcher(_dispatch_local_post)
    rooms_svc.set_remote_scope_dispatcher(_dispatch_remote_scope)
    rooms_svc.set_shadow_peer_dispatcher(_dispatch_shadow_peer)
    rooms_svc.set_close_room_kill_callback(_on_close_room_kill)


# Auto-wire on import — the rooms service is import-time safe (no side
# effects beyond module-level state).
install_room_dispatchers()
