"""Agent instance management — rooms-aware, tracked, addressable.

An AgentInstance owns one ``claude --input-format=stream-json
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

from awm import config
from awm.config import PROJECTS_DIR
from awm.db import get_connection
from awm.models import AgentSessionInfo
from awm.services import rooms as rooms_svc
from awm.services._path import resolve_bin


_SUPPORTED_CLIS = {"claude", "opencode"}
_INPUT_QUEUE_SIZE = 128

_ACTIVE_STATUSES = ("starting", "running", "stopping", "orphaned")


def _scope_key(project: str, scope: str) -> str:
    return f"{project}/{scope}"


# ---------------------------------------------------------------------------
# AgentInstance
# ---------------------------------------------------------------------------

class AgentInstance:
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
        # Spawn-time flags + captured claude session id (from init event).
        # Resume preserves server-side conversation state across respawns.
        self.permission_mode: str = "default"
        self.model: Optional[str] = None
        self.effort: Optional[str] = None
        self.claude_session_id: Optional[str] = None
        # Slash commands advertised by claude in the init event (per-scope).
        self.claude_slash_commands: list[str] = []
        # Context-window telemetry, populated from stream-json events.
        # ``context_used`` is the cumulative input tokens fed to the next
        # turn (input + cache_read + cache_creation). ``context_max`` is
        # heuristically derived from the model id reported at init.
        self.context_used: int = 0
        self.context_max: Optional[int] = None
        # In-flight respawn lock so concurrent /restart calls don't fight.
        self.respawn_lock: asyncio.Lock = asyncio.Lock()


_registry: dict[int, AgentInstance] = {}
_by_scope: dict[str, AgentInstance] = {}
_registry_lock = asyncio.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ScopeBusyError(Exception):
    """A second AgentInstance spawn for an already-running scope was attempted."""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

_VALID_PERMISSION_MODES = (
    "default", "acceptEdits", "auto", "bypassPermissions", "dontAsk", "plan",
)
_VALID_EFFORTS = ("low", "medium", "high", "xhigh", "max")


def _build_claude_argv(
    *, permission_mode: str, model: Optional[str], effort: Optional[str],
    resume_session_id: Optional[str],
) -> list[str]:
    argv = [
        resolve_bin("claude"), "--print", "--verbose",
        "--input-format=stream-json", "--output-format=stream-json",
        "--include-partial-messages",
        f"--permission-mode={permission_mode}",
    ]
    if model:
        argv.extend(["--model", model])
    if effort:
        argv.extend(["--effort", effort])
    if resume_session_id:
        argv.extend(["--resume", resume_session_id])
    # Pin MCP config to the exposed-server-written spawn-mcp.json so dev
    # and prod can't bleed into each other and so spawned agents see the
    # canonical MCP catalog. Missing-file case (awm-exposed never ran)
    # falls through with no flags.
    spawn_mcp = config.AWM_DIR / "spawn-mcp.json"
    if spawn_mcp.exists():
        argv.extend(["--strict-mcp-config", "--mcp-config", str(spawn_mcp)])
    return argv


def _build_opencode_argv(
    *, workspace_dir: Path, permission_mode: str, model: Optional[str],
) -> list[str]:
    # Stdin/event bridging for opencode is a follow-up (see plan
    # ``we-want-to-update-humble-clarke``). The argv below launches the
    # CLI correctly; full room-driven I/O parity with the claude harness
    # is not wired here.
    argv = [resolve_bin("opencode"), "run", "--format", "json",
            "--dir", str(workspace_dir)]
    if permission_mode == "bypassPermissions":
        argv.append("--dangerously-skip-permissions")
    if model:
        argv.extend(["--model", model])
    return argv


async def create_session(*, project: str, scope: str,
                         agent_cli: str = "claude",
                         permission_mode: str = "default",
                         model: Optional[str] = None,
                         effort: Optional[str] = None,
                         resume_session_id: Optional[str] = None) -> AgentInstance:
    """Spawn a claude subprocess and register it. Raises ScopeBusyError
    if the scope already has an active session."""
    if agent_cli not in _SUPPORTED_CLIS:
        raise ValueError(
            f"Unknown agent CLI '{agent_cli}'. Live sessions require: {sorted(_SUPPORTED_CLIS)}"
        )
    if permission_mode not in _VALID_PERMISSION_MODES:
        raise ValueError(
            f"Invalid permission_mode {permission_mode!r}; "
            f"choices: {list(_VALID_PERMISSION_MODES)}"
        )
    if effort is not None and effort not in _VALID_EFFORTS:
        raise ValueError(
            f"Invalid effort {effort!r}; choices: {list(_VALID_EFFORTS)}"
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
            # If no resume id was passed, recover the most recent one this
            # scope captured in a prior life. Lets re-invite-after-death
            # still pass --resume even though _by_scope was reaped.
            if resume_session_id is None:
                row = conn.execute(
                    "SELECT claude_session_id FROM agent_sessions "
                    "WHERE project=? AND scope=? AND claude_session_id IS NOT NULL "
                    "ORDER BY id DESC LIMIT 1",
                    (project, scope),
                ).fetchone()
                if row is not None:
                    resume_session_id = row["claude_session_id"]
            try:
                cur = conn.execute(
                    "INSERT INTO agent_sessions "
                    "(project, scope, pid, status, agent_cli, started_at, log_path, claude_session_id) "
                    "VALUES (?, ?, 0, 'starting', ?, ?, ?, ?)",
                    (project, scope, agent_cli, started_at, str(log_path),
                     resume_session_id),
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

        # Spawn the subprocess. Harness selection is per-session via the
        # ``agent_cli`` column. opencode reads MCP config + scope
        # ``instructions`` from a per-scope ``<worktree>/.awm/mcp-opencode.json``
        # (written at scope-create time by ``_write_scope_opencode_config``),
        # which inlines ``instructions: [".awm/context.md"]`` so opencode
        # auto-loads the scope's context. Falls back to the workspace-level
        # ``<workspace>/.awm/mcp-opencode.json`` for sessions outside scopes
        # (or pre-heal worktrees).
        spawn_env: dict[str, str] | None = None
        if agent_cli == "opencode":
            argv = _build_opencode_argv(
                workspace_dir=workspace_dir,
                permission_mode=permission_mode, model=model,
            )
            scope_opencode_cfg = awm_dir / "mcp-opencode.json"
            workspace_opencode_cfg = config.AWM_DIR / "mcp-opencode.json"
            opencode_cfg = (
                scope_opencode_cfg if scope_opencode_cfg.exists()
                else workspace_opencode_cfg
            )
            if opencode_cfg.exists():
                spawn_env = {**os.environ, "OPENCODE_CONFIG": str(opencode_cfg)}
        else:
            argv = _build_claude_argv(
                permission_mode=permission_mode, model=model, effort=effort,
                resume_session_id=resume_session_id,
            )
        log_fp = open(log_path, "ab")
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                cwd=str(workspace_dir),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=log_fp,
                start_new_session=True,
                env=spawn_env,
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
            raise RuntimeError(f"{agent_cli} binary not on PATH: {exc}") from exc
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

        session = AgentInstance(
            id=session_id, project=project, scope=scope,
            agent_cli=agent_cli, log_path=log_path, proc=proc,
        )
        session.started_at = started_at
        session.permission_mode = permission_mode
        session.model = model
        session.effort = effort
        # Optimistically reuse the resume id; the init event will overwrite
        # it with the authoritative one claude reports.
        session.claude_session_id = resume_session_id
        _registry[session_id] = session
        _by_scope[key] = session

    # Background pumps (outside the lock).
    session.reader_task = asyncio.create_task(_reader_loop(session))
    session.waiter_task = asyncio.create_task(_waiter_loop(session))
    session.input_pump_task = asyncio.create_task(_input_pump(session))
    return session


def get_session_by_scope(project: str, scope: str) -> AgentInstance | None:
    return _by_scope.get(_scope_key(project, scope))


def get_session(session_id: int) -> AgentInstance | None:
    return _registry.get(session_id)


# ---------------------------------------------------------------------------
# Input pump — serialize per-room posts into stdin
# ---------------------------------------------------------------------------

async def _input_pump(session: AgentInstance) -> None:
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


def enqueue_input(session: AgentInstance, room_id: str, post: rooms_svc.Post) -> bool:
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

def _lookup_context_max(model_id: Optional[str]) -> Optional[int]:
    """Heuristic context-window size for a claude model id.

    The id format used by claude-code is e.g. ``claude-opus-4-7`` or
    ``claude-opus-4-7[1m]``. We match on substrings rather than exact
    ids so newer minor versions keep working without a code change.
    Returns ``None`` if the model is unknown — the UI hides the bar.
    """
    if not isinstance(model_id, str) or not model_id:
        return None
    m = model_id.lower()
    if "[1m]" in m:
        return 1_000_000
    if "opus" in m or "sonnet" in m or "haiku" in m:
        return 200_000
    return None


def _update_usage_from_event(session: "AgentInstance", parsed: dict) -> None:
    """Best-effort extraction of token usage from a stream-json event.

    Stream-json assistant messages carry a ``usage`` block with
    ``input_tokens``, ``output_tokens``, ``cache_creation_input_tokens``,
    ``cache_read_input_tokens``. We treat the sum of the three input-
    side fields as ``context_used`` — that's the size of the prompt
    fed into the next turn, which is what an operator cares about
    when deciding whether to /compact.

    Schema may drift across claude versions; on any failure we leave
    ``context_used`` unchanged rather than zeroing or crashing.
    """
    try:
        usage = None
        if parsed.get("type") == "assistant":
            usage = parsed.get("message", {}).get("usage")
        elif parsed.get("type") == "result":
            usage = parsed.get("usage")
        if not isinstance(usage, dict):
            return
        inp = usage.get("input_tokens") or 0
        cache_read = usage.get("cache_read_input_tokens") or 0
        cache_create = usage.get("cache_creation_input_tokens") or 0
        total = int(inp) + int(cache_read) + int(cache_create)
        if total > 0:
            session.context_used = total
    except (TypeError, ValueError, AttributeError):
        return


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
        # The successful "result" event repeats the final assistant text we
        # already posted from the preceding "assistant" event, so skip it.
        # Errors aren't carried in any assistant event, so surface them here.
        if parsed.get("is_error"):
            body = parsed.get("result")
            if isinstance(body, str) and body.strip():
                out.append(("system", f"[error] {body.strip()}"))
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


async def _reader_loop(session: AgentInstance) -> None:
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

        # Capture init metadata so /restart can resume + the UI knows what
        # slash commands this scope's claude exposes.
        if parsed.get("type") == "system" and parsed.get("subtype") == "init":
            sid = parsed.get("session_id")
            if isinstance(sid, str) and sid != session.claude_session_id:
                session.claude_session_id = sid
                # Persist so re-invite after death can still --resume.
                conn = get_connection()
                try:
                    conn.execute(
                        "UPDATE agent_sessions SET claude_session_id=? WHERE id=?",
                        (sid, session.id),
                    )
                    conn.commit()
                finally:
                    conn.close()
            cmds = parsed.get("slash_commands")
            if isinstance(cmds, list):
                session.claude_slash_commands = [c for c in cmds if isinstance(c, str)]
            # Capture context-window from init's reported model. Fall
            # back to the spawn-arg model if init doesn't carry one.
            init_model = parsed.get("model") or session.model
            session.context_max = _lookup_context_max(init_model)

        _update_usage_from_event(session, parsed)

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

async def _waiter_loop(session: AgentInstance) -> None:
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
# Respawn + slash-input primitives
# ---------------------------------------------------------------------------

class NoSessionError(Exception):
    """No agent instance exists for the requested scope."""


async def respawn_session(
    scope_key: str, *,
    force: bool = False,
    permission_mode: Optional[str] = None,
    model: Optional[str] = None,
    effort: Optional[str] = None,
) -> AgentInstance:
    """Kill the current AgentInstance for ``scope_key`` and spawn a fresh one
    with ``--resume <claude_session_id>`` so conversation context is
    preserved server-side.

    Any of permission_mode/model/effort that aren't passed inherit the
    current session's values. ``force=True`` uses SIGKILL; otherwise
    SIGTERM with a short drain wait.
    """
    current = _by_scope.get(scope_key)
    if current is None:
        raise NoSessionError(f"no active session for {scope_key}")

    async with current.respawn_lock:
        new_mode = permission_mode if permission_mode is not None else current.permission_mode
        new_model = model if model is not None else current.model
        new_effort = effort if effort is not None else current.effort
        resume_sid = current.claude_session_id
        project, scope = current.project, current.scope

        # Tear down the old subprocess and wait for the waiter loop to
        # de-register it from _by_scope before spawning the replacement.
        if force:
            await kill_session(current.id)
        else:
            await stop_session(current.id)
        # _waiter_loop removes from _by_scope on exit; bounded wait.
        for _ in range(60):  # up to ~3s
            if _by_scope.get(scope_key) is None:
                break
            await asyncio.sleep(0.05)
        else:
            # Drain didn't complete — escalate.
            await kill_session(current.id)
            for _ in range(40):
                if _by_scope.get(scope_key) is None:
                    break
                await asyncio.sleep(0.05)

    return await create_session(
        project=project, scope=scope,
        permission_mode=new_mode, model=new_model, effort=new_effort,
        resume_session_id=resume_sid,
    )


async def send_slash(scope_key: str, body: str) -> None:
    """Write a raw stream-json user message containing ``body`` directly to
    the scope's claude stdin, bypassing the room-framing pump.

    Slash commands like /clear or /compact MUST start the user message at
    position 0 to be recognized as commands. The standard input pump's
    ``[room:X from:Y]\\n`` prefix breaks that, so this path exists.
    """
    session = _by_scope.get(scope_key)
    if session is None:
        raise NoSessionError(f"no active session for {scope_key}")
    if session.proc is None or session.proc.stdin is None:
        raise NoSessionError(f"session for {scope_key} has no stdin")
    if session.proc.stdin.is_closing():
        raise NoSessionError(f"session for {scope_key} stdin is closing")
    payload = {
        "type": "user",
        "message": {"role": "user", "content": body},
    }
    line = (json.dumps(payload) + "\n").encode("utf-8")
    session.proc.stdin.write(line)
    await session.proc.stdin.drain()
    try:
        with session.stdin_frames_log.open("a", encoding="utf-8") as fp:
            fp.write(f"STDIN(slash) {body!r}\n")
    except OSError:
        pass


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
    """Wire the rooms service to push input frames into AgentInstances and
    to kill agents on close_room(..., kill_agents=True)."""
    rooms_svc.set_local_scope_dispatcher(_dispatch_local_post)
    rooms_svc.set_remote_scope_dispatcher(_dispatch_remote_scope)
    rooms_svc.set_shadow_peer_dispatcher(_dispatch_shadow_peer)
    rooms_svc.set_close_room_kill_callback(_on_close_room_kill)


# Auto-wire on import — the rooms service is import-time safe (no side
# effects beyond module-level state).
install_room_dispatchers()
