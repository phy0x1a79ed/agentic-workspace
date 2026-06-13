"""Agent instance management — rooms-aware, tracked, addressable (modular v1).

An AgentInstance owns one ``claude`` or ``opencode`` subprocess attached to a
single ``(project, scope)``. Its job is the *agent runtime*: serialize inputs
from any number of rooms into stdin, parse stdout, and broadcast text/tool
events into the agent's owned room transcript.

Modular changes from the monolith
----------------------------------
- **Identity** is resolved via gatewayclient calls to the ``scopes`` service.
  No uuid ``agent_id`` is stored or referenced; ``(project, scope)`` is the
  natural key throughout.
- **Persistence** goes through ``AgentsDAO`` → ``agents.db``; no shared
  ``state.db``.
- **Room operations** are cross-service; they go via gatewayclient to the
  ``scopes`` service, which exposes the room RPCs in its ready manifest:
  ``ensureAgentRoom`` (owned-room get-or-create), ``roomsForScope`` (active
  rooms for output broadcast), ``room_post`` (post a transcript event),
  ``autoCloseForScope`` (close the scope's rooms on session end), plus
  ``room_create``/``room_invite`` used by ``orchestration``.
- **Transcript writes** go to the ``agent_transcript`` table in ``agents.db``
  via ``agent_transcript.record_*`` (which uses AgentsDAO).

REMAINING CROSS-SERVICE ITEM
----------------------------
``roomAgentsKillOnClose(room_id)`` — when a room closes with ``kill_agents``,
the scopes service knows which agent scopes to SIGTERM, but the kill happens
in *this* process. That reverse notification (scopes → agents) needs a pub/sub
or callback channel and is intentionally NOT invented here; it is the one
deferred integration item. All forward (agents → scopes) room calls are wired.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
from pathlib import Path
from typing import Optional

from awm import config
from awm.config import PROJECTS_DIR
from awm.agents import dao as _dao_module
from awm.agents.dao import AgentsDAO
from awm.agents._time import now_ms, ms_to_iso, iso_to_ms, SYSTEM_REF
from awm.agents._path import resolve_bin
from awm.agents.models import AgentSessionInfo
import awm.gatewayclient as gatewayclient


_SUPPORTED_CLIS = {"claude", "opencode"}
_INPUT_QUEUE_SIZE = 128

# Module-level DAO instance (initialized after dao.init() is called).
_dao: AgentsDAO | None = None


def _get_dao() -> AgentsDAO:
    global _dao
    if _dao is None:
        _dao = AgentsDAO()
    return _dao


def _scope_key(project: str, scope: str) -> str:
    return f"{project}/{scope}"


# ---------------------------------------------------------------------------
# AgentInstance
# ---------------------------------------------------------------------------

class AgentInstance:
    """In-memory handle for a running CLI subprocess.

    ``id`` is the ``agent_instances.id`` (per-spawn integer). ``project`` and
    ``scope`` are the natural key — no ``agent_id`` uuid is stored.
    """

    def __init__(
        self,
        *,
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
        self.started_at_ms: int = now_ms()
        self.exited_at_ms: Optional[int] = None
        self.exit_code: Optional[int] = None
        self.input_queue: asyncio.Queue = asyncio.Queue(maxsize=_INPUT_QUEUE_SIZE)
        self.reader_task: Optional[asyncio.Task] = None
        self.waiter_task: Optional[asyncio.Task] = None
        self.input_pump_task: Optional[asyncio.Task] = None
        self.stdin_ready: asyncio.Event = asyncio.Event()
        self.stdin_frames_log = log_path.parent / "agent.log"
        self.permission_mode: str = "default"
        self.model: Optional[str] = None
        self.effort: Optional[str] = None
        self.cli_session_id: Optional[str] = None
        self.claude_slash_commands: list[str] = []
        self.context_used: int = 0
        self.context_max: Optional[int] = None
        self.respawn_lock: asyncio.Lock = asyncio.Lock()
        self.compacting: bool = False

    @property
    def claude_session_id(self) -> Optional[str]:
        return self.cli_session_id

    @claude_session_id.setter
    def claude_session_id(self, value: Optional[str]) -> None:
        self.cli_session_id = value


# In-memory registries keyed on scope_key (project/scope) and instance id.
_registry_by_id: dict[int, AgentInstance] = {}
_by_scope: dict[str, AgentInstance] = {}
_registry_lock = asyncio.Lock()

# Resume wake schedule — scope_key → unix-ms.
_resume_schedule: dict[str, int] = {}


class ScopeBusyError(Exception):
    """A second AgentInstance spawn for an already-running scope was attempted."""


class NoSessionError(Exception):
    """No agent instance exists for the requested scope."""


# ---------------------------------------------------------------------------
# Identity helpers (scopes RPC)
# ---------------------------------------------------------------------------

_ref_cache = gatewayclient.RefCache(ttl=120.0)


async def _ensure_scope_exists(project: str, scope: str,
                                agent_cli: str,
                                worktree: Path,
                                as_: str | None = None) -> None:
    """Ensure project + scope exist in the scopes service.

    Calls ensureProject then ensureScope via gatewayclient. Does not return
    an agent_id — the natural key (project, scope) is the identity.
    """
    bare = PROJECTS_DIR / project / ".bare"
    await gatewayclient.call(
        "scopes", "ensureProject",
        {"project": project, "repo_path": str(bare)},
        as_=as_,
    )
    await gatewayclient.call(
        "scopes", "ensureScope",
        {
            "project": project, "scope": scope,
            "branch": f"feat/{scope}",
            "worktree": str(worktree),
            "agent_cli": agent_cli,
            "status": "active",
        },
        as_=as_,
    )


# ---------------------------------------------------------------------------
# Rooms helpers (STUBBED — blocked on scopes RPC contract extension)
# ---------------------------------------------------------------------------

async def _ensure_owned_room(*, project: str, scope: str) -> str | None:
    """Get-or-create the agent's owned room via the scopes service."""
    result = await gatewayclient.call(
        'scopes', 'ensureAgentRoom', {'project': project, 'scope': scope})
    return result.get('room_id') if result else None


async def _rooms_for_scope(scope_key: str) -> list[str]:
    """Active rooms involving scope_key, via the scopes service."""
    project, _, scope = scope_key.partition("/")
    if not scope:
        return []
    result = await gatewayclient.call(
        'scopes', 'roomsForScope', {'project': project, 'scope': scope})
    return result.get('rooms', []) if result else []


# ---------------------------------------------------------------------------
# Subprocess argv builders
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
    spawn_mcp = config.AWM_DIR / "spawn-mcp.json"
    if spawn_mcp.exists():
        argv.extend(["--strict-mcp-config", "--mcp-config", str(spawn_mcp)])
    return argv


def _build_opencode_argv(
    *, workspace_dir: Path, permission_mode: str, model: Optional[str],
) -> list[str]:
    argv = [resolve_bin("opencode"), "run", "--format", "json",
            "--dir", str(workspace_dir)]
    if permission_mode == "bypassPermissions":
        argv.append("--dangerously-skip-permissions")
    if model:
        argv.extend(["--model", model])
    return argv


# ---------------------------------------------------------------------------
# Public API: create / lookup
# ---------------------------------------------------------------------------

async def create_session(*, project: str, scope: str,
                         agent_cli: str = "claude",
                         permission_mode: str = "default",
                         model: Optional[str] = None,
                         effort: Optional[str] = None,
                         resume_session_id: Optional[str] = None,
                         fresh: bool = False) -> AgentInstance:
    """Spawn a CLI subprocess and register it."""
    if agent_cli not in _SUPPORTED_CLIS:
        raise ValueError(
            f"Unknown agent CLI '{agent_cli}'. Supported: {sorted(_SUPPORTED_CLIS)}"
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
    workspace_dir = PROJECTS_DIR / project / scope
    if not workspace_dir.exists():
        raise FileNotFoundError(f"Scope workspace not found at {workspace_dir}")
    awm_dir = workspace_dir / ".awm"
    awm_dir.mkdir(parents=True, exist_ok=True)
    log_path = awm_dir / "session.log"

    async with _registry_lock:
        if key in _by_scope:
            raise ScopeBusyError(
                f"scope {key} already has an active session "
                f"(pid={_by_scope[key].proc.pid})"
            )

        # Ensure project + scope exist in the scopes service.
        try:
            await _ensure_scope_exists(
                project=project, scope=scope,
                agent_cli=agent_cli, worktree=workspace_dir,
            )
        except gatewayclient.GatewayCallError as exc:
            raise RuntimeError(
                f"Failed to ensure scope {project}/{scope} via scopes RPC: {exc}"
            ) from exc

        dao = _get_dao()
        # Resume id recovery.
        if resume_session_id is None and not fresh:
            resume_session_id = dao.get_latest_cli_session_id(project, scope)

        instance_id = dao.open_instance(
            project=project, scope=scope,
            log_path=str(log_path),
            cli_session_id=resume_session_id,
            started_at=now_ms(),
        )

        # Ensure the owned room exists (stubbed until scopes exposes RPC).
        try:
            await _ensure_owned_room(project=project, scope=scope)
        except Exception:  # noqa: BLE001
            pass

        # Spawn the subprocess.
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
            dao.close_instance(instance_id, ended_at=now_ms(), exit_code=-1,
                               intent_override="failed_to_spawn")
            raise RuntimeError(f"{agent_cli} binary not on PATH: {exc}") from exc
        finally:
            log_fp.close()

        session = AgentInstance(
            id=instance_id,
            project=project,
            scope=scope,
            agent_cli=agent_cli,
            log_path=log_path,
            proc=proc,
        )
        session.permission_mode = permission_mode
        session.model = model
        session.effort = effort
        session.cli_session_id = resume_session_id
        _registry_by_id[instance_id] = session
        _by_scope[key] = session

    session.reader_task = asyncio.create_task(_reader_loop(session))
    session.waiter_task = asyncio.create_task(_waiter_loop(session))
    session.input_pump_task = asyncio.create_task(_input_pump(session))
    try:
        await asyncio.wait_for(session.stdin_ready.wait(), timeout=10.0)
    except asyncio.TimeoutError:
        try:
            session.proc.kill()
        except ProcessLookupError:
            pass
        async with _registry_lock:
            _by_scope.pop(key, None)
            _registry_by_id.pop(instance_id, None)
        _get_dao().close_instance(instance_id, ended_at=now_ms(), exit_code=-1,
                                   intent_override="stdin_ready_timeout")
        raise RuntimeError(
            f"input pump never signaled stdin-ready for {key} within 10s"
        )
    return session


def get_session_by_scope(project: str, scope: str) -> AgentInstance | None:
    return _by_scope.get(_scope_key(project, scope))


def get_session(session_id: int) -> AgentInstance | None:
    return _registry_by_id.get(session_id)


# ---------------------------------------------------------------------------
# Input pump
# ---------------------------------------------------------------------------

async def _input_pump(session: AgentInstance) -> None:
    session.stdin_ready.set()
    if (session.proc is None or session.proc.stdin is None
            or session.proc.stdin.is_closing()):
        return
    while True:
        try:
            room_id, post_author, post_body = await session.input_queue.get()
        except asyncio.CancelledError:
            return
        if session.proc is None or session.proc.stdin is None:
            return
        if session.proc.stdin.is_closing():
            return
        framed_body = f"[room:{room_id} from:{post_author}]\n{post_body}"
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
        try:
            with session.stdin_frames_log.open("a", encoding="utf-8") as fp:
                fp.write(f"STDIN {framed_body!r}\n")
        except OSError:
            pass
        # record_in signature: (session, body, *, injection=False)
        from awm.agents import agent_transcript
        agent_transcript.record_in(session, framed_body, injection=False)


def enqueue_input(session: AgentInstance, room_id: str,
                  post_author: str, post_body: str) -> bool:
    """Enqueue a room post for the agent's stdin pump."""
    try:
        session.input_queue.put_nowait((room_id, post_author, post_body))
        return True
    except asyncio.QueueFull:
        return False


# ---------------------------------------------------------------------------
# Output reader
# ---------------------------------------------------------------------------

def _lookup_context_max(model_id: Optional[str]) -> Optional[int]:
    if not isinstance(model_id, str) or not model_id:
        return None
    m = model_id.lower()
    if "[1m]" in m:
        return 1_000_000
    if "opus" in m or "sonnet" in m or "haiku" in m:
        return 200_000
    return None


def _update_usage_from_event(session: "AgentInstance", parsed: dict) -> None:
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
                    out.append(("message", text))
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
        if parsed.get("is_error"):
            body = parsed.get("result")
            if isinstance(body, str) and body.strip():
                out.append(("system", f"[error] {body.strip()}"))
    return out


async def _reader_loop(session: AgentInstance) -> None:
    from awm.agents import agent_transcript
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
            try:
                with session.stdin_frames_log.open("a", encoding="utf-8") as fp:
                    fp.write(f"STDOUT(raw) {text}\n")
            except OSError:
                pass
            agent_transcript.record_raw_out(session, text)
            continue

        agent_transcript.record_out(session, parsed)

        if parsed.get("type") == "system" and parsed.get("subtype") == "init":
            sid = parsed.get("session_id")
            if isinstance(sid, str) and sid != session.cli_session_id:
                session.cli_session_id = sid
                _get_dao().update_instance_cli_session_id(session.id, sid)
            cmds = parsed.get("slash_commands")
            if isinstance(cmds, list):
                session.claude_slash_commands = [c for c in cmds if isinstance(c, str)]
            init_model = parsed.get("model") or session.model
            session.context_max = _lookup_context_max(init_model)

        _update_usage_from_event(session, parsed)

        events = _extract_renderable(parsed)
        if not events:
            continue

        # Broadcast to the scope's rooms via the scopes service.
        try:
            rooms = await _rooms_for_scope(session.scope_key)
        except Exception:  # noqa: BLE001
            rooms = []
        if not rooms:
            continue
        author_ref = f"agent:{session.project}/{session.scope}"
        for room_id in rooms:
            for kind, body in events:
                try:
                    await gatewayclient.call('scopes', 'room_post', {
                        'room_id': room_id, 'author': author_ref,
                        'body': body, 'kind': kind})
                except Exception:  # noqa: BLE001
                    pass


# ---------------------------------------------------------------------------
# Waiter
# ---------------------------------------------------------------------------

async def _waiter_loop(session: AgentInstance) -> None:
    assert session.proc is not None
    exit_code = await session.proc.wait()
    session.exit_code = exit_code
    session.exited_at_ms = now_ms()
    final = "killed" if session.status == "killed" else "exited"
    session.status = final

    _get_dao().close_instance(session.id, ended_at=now_ms(), exit_code=exit_code)

    if session.input_pump_task is not None:
        session.input_pump_task.cancel()

    async with _registry_lock:
        if _by_scope.get(session.scope_key) is session:
            _by_scope.pop(session.scope_key, None)
        _registry_by_id.pop(session.id, None)

    # Auto-close the scope's owned rooms now that its session has ended.
    project, _, scope = session.scope_key.partition("/")
    if scope:
        try:
            await gatewayclient.call(
                'scopes', 'autoCloseForScope', {'project': project, 'scope': scope})
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
# Stop / kill
# ---------------------------------------------------------------------------

def _info_for_instance_row(row: dict) -> AgentSessionInfo:
    return AgentSessionInfo(
        id=row["id"],
        project=row["project"],
        scope=row["scope"],
        pid=row.get("pid") or 0,
        status=row.get("render_status") or "exited",
        agent_cli=row.get("agent_cli") or "claude",
        started_at=ms_to_iso(row["started_at"]) or "",
        exited_at=ms_to_iso(row.get("ended_at")),
        exit_code=row.get("exit_code"),
        attached=False,
    )


def _render_status(row: dict) -> str:
    if row.get("ended_at") is None:
        return "running" if row["id"] in _registry_by_id else "orphaned"
    try:
        data = json.loads(row.get("data") or "{}")
    except (TypeError, ValueError):
        data = {}
    intent = data.get("intent") or "live"
    if intent == "killed":
        return "killed"
    if intent in ("stopped", "compacted"):
        return "exited"
    return "exited"


def _hydrate_instance_row(row: dict) -> dict:
    instance_handle = _registry_by_id.get(row["id"])
    pid = instance_handle.proc.pid if instance_handle else 0
    try:
        data = json.loads(row.get("data") or "{}")
    except (TypeError, ValueError):
        data = {}
    out = dict(row)
    out["pid"] = pid
    out["exit_code"] = data.get("exit_code")
    out["render_status"] = _render_status(out)
    # agent_cli: not in agents.db; default to "claude" (or recover from data)
    out.setdefault("agent_cli", data.get("agent_cli") or "claude")
    return out


def _row_for_instance(instance_id: int) -> dict | None:
    row = _get_dao().get_instance(instance_id)
    if row is None:
        return None
    return _hydrate_instance_row(row)


async def stop_session(session_id: int) -> AgentSessionInfo:
    session = _registry_by_id.get(session_id)
    row = _row_for_instance(session_id)
    if row is None:
        raise FileNotFoundError(f"session {session_id} not found")
    if row.get("ended_at") is not None:
        return _info_for_instance_row(row)
    _get_dao().set_instance_intent(session_id, "stopped")
    if session is None:
        return _info_for_instance_row(_row_for_instance(session_id) or row)
    session.status = "stopping"
    try:
        session.proc.terminate()
    except ProcessLookupError:
        pass
    return _info_for_instance_row(_row_for_instance(session_id) or row)


async def kill_session(session_id: int) -> AgentSessionInfo:
    session = _registry_by_id.get(session_id)
    row = _row_for_instance(session_id)
    if row is None:
        raise FileNotFoundError(f"session {session_id} not found")
    if row.get("ended_at") is not None:
        return _info_for_instance_row(row)
    _get_dao().set_instance_intent(session_id, "killed")
    if session is None:
        return _info_for_instance_row(_row_for_instance(session_id) or row)
    session.status = "killed"
    try:
        session.proc.kill()
    except ProcessLookupError:
        pass
    return _info_for_instance_row(_row_for_instance(session_id) or row)


# ---------------------------------------------------------------------------
# Respawn + slash-input primitives
# ---------------------------------------------------------------------------

async def respawn_session(
    scope_key: str, *,
    force: bool = False,
    permission_mode: Optional[str] = None,
    model: Optional[str] = None,
    effort: Optional[str] = None,
    agent_cli: Optional[str] = None,
    clear_history: bool = False,
) -> AgentInstance:
    current = _by_scope.get(scope_key)
    if current is None:
        raise NoSessionError(f"no active session for {scope_key}")

    async with current.respawn_lock:
        new_mode = permission_mode if permission_mode is not None else current.permission_mode
        new_model = model if model is not None else current.model
        new_effort = effort if effort is not None else current.effort
        new_cli = agent_cli if agent_cli is not None else current.agent_cli
        resume_sid = None if clear_history else current.cli_session_id
        project, scope = current.project, current.scope

        if force:
            await kill_session(current.id)
        else:
            await stop_session(current.id)
        for _ in range(60):
            if _by_scope.get(scope_key) is None:
                break
            await asyncio.sleep(0.05)
        else:
            await kill_session(current.id)
            for _ in range(40):
                if _by_scope.get(scope_key) is None:
                    break
                await asyncio.sleep(0.05)

    return await create_session(
        project=project, scope=scope,
        agent_cli=new_cli,
        permission_mode=new_mode, model=new_model, effort=new_effort,
        resume_session_id=resume_sid,
        fresh=clear_history,
    )


async def send_slash(scope_key: str, body: str) -> None:
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
    from awm.agents import agent_transcript
    agent_transcript.record_in(session, body, injection=True)


# ---------------------------------------------------------------------------
# Compact
# ---------------------------------------------------------------------------

_COMPACT_PROMPT = (
    "Please summarize our entire conversation so far in a single message, "
    "optimized to be a primer for a fresh agent that needs to continue this "
    "work. Include: current task, key decisions, files touched, open "
    "questions, and the next action you were about to take. Reply with ONLY "
    "the summary, no preamble."
)

COMPACT_TIMEOUT_S = 180.0
COMPACT_MIN_SUMMARY_CHARS = 50


async def compact_session(scope_key: str) -> str:
    from awm.agents import agent_transcript
    session = _by_scope.get(scope_key)
    if session is None:
        raise NoSessionError(f"no active session for {scope_key}")
    if session.agent_cli != "claude":
        return (
            f"/compact gated for agent_cli={session.agent_cli!r}; "
            "opencode bridging is a separate plan"
        )
    if session.compacting:
        return f"/compact already in flight for {scope_key}"
    if agent_transcript.has_unmatched_tool_use(session.project, session.scope):
        return (
            f"compact refused: tool call in flight on {scope_key}; "
            "wait for completion or /kill first"
        )

    session.compacting = True
    prior_instance_id = session.id
    queue = agent_transcript.subscribe_assistant_turns(prior_instance_id)
    summary: str = ""
    try:
        try:
            await send_slash(scope_key, _COMPACT_PROMPT)
        except NoSessionError as exc:
            return f"compact failed: {exc}"

        try:
            summary = await asyncio.wait_for(queue.get(), timeout=COMPACT_TIMEOUT_S)
        except asyncio.TimeoutError:
            summary = agent_transcript.read_recent_assistant_text(session.project, session.scope)
        if not summary or len(summary) < COMPACT_MIN_SUMMARY_CHARS:
            return (
                f"compact failed: no usable summary captured "
                f"(got {len(summary)} chars); session untouched"
            )

        _get_dao().set_instance_intent(prior_instance_id, "compacted")

        try:
            new_session = await respawn_session(scope_key, clear_history=True)
        except Exception as exc:  # noqa: BLE001
            return (
                f"compact respawn failed: {exc}; previous-session summary "
                f"available via agent transcript (len {len(summary)})"
            )

        primer = f"[system: previous-session-summary]\n{summary}"
        try:
            await send_slash(scope_key, primer)
        except NoSessionError as exc:
            return (
                f"compact primer injection failed: {exc}; session "
                f"summary still available via transcript"
            )

        # Audit notice — room broadcast stubbed.
        return (
            f"compacted {scope_key}: prior instance {prior_instance_id} "
            f"summarized → new instance {new_session.id} primed"
        )
    finally:
        agent_transcript.unsubscribe_assistant_turns(prior_instance_id, queue)
        session.compacting = False


# ---------------------------------------------------------------------------
# Query helpers
# ---------------------------------------------------------------------------

def list_sessions(project: str | None = None, scope: str | None = None,
                  status: str | None = None) -> list[AgentSessionInfo]:
    rows = _get_dao().list_instances(project=project, scope=scope)
    out: list[AgentSessionInfo] = []
    for r in rows:
        hydrated = _hydrate_instance_row(r)
        if status and hydrated["render_status"] != status:
            continue
        out.append(_info_for_instance_row(hydrated))
    return out


def tail_log(session_id: int, lines: int = 200) -> str:
    row = _row_for_instance(session_id)
    if row is None:
        raise FileNotFoundError(f"session {session_id} not found")
    path = Path(row.get("log_path") or "")
    if not path or not path.exists():
        return ""
    with path.open("r", encoding="utf-8", errors="replace") as fp:
        data = fp.readlines()
    return "".join(data[-lines:])


def scrub_resume_queue_for_scope(project: str, scope: str) -> int:
    """Clear in-memory resume schedule for a scope. No-op if not scheduled."""
    key = _scope_key(project, scope)
    return 1 if _resume_schedule.pop(key, None) is not None else 0


# ---------------------------------------------------------------------------
# Reconciliation on startup
# ---------------------------------------------------------------------------

def reconcile_on_startup() -> None:
    """Close open agent_instances rows and seed resume schedule."""
    dao = _get_dao()
    dao.close_all_open_instances(now_ms())

    # Retire vagrant agents: STUB — vagrant status lives in scopes DB.
    # The scopes service handles this in its own reconcile.

    # Schedule resume for scopes that were recently active (had an open row).
    # In v1 we don't have a separate agents.status column — rely on scopes RPC
    # for true status. Seed resume for any scope that had an open instance row.
    recently_open = dao.get_active_scopes()  # returns scopes with cli_session_id
    for r in recently_open:
        scope_key = _scope_key(r["project"], r["scope"])
        _resume_schedule[scope_key] = now_ms()


# ---------------------------------------------------------------------------
# Auto-resume driver
# ---------------------------------------------------------------------------

RESUME_HEALTH_WINDOW_S = 15.0
MAX_REPLAY_POSTS = 200
MAX_CONCURRENT_RESUMES = 3
RESUME_DRIVER_POLL_S = 5.0
RESUME_MAX_ATTEMPTS = 3

_resume_attempts: dict[str, int] = {}
_resume_driver_task: asyncio.Task | None = None


def _due_resume_scope_keys() -> list[str]:
    now = now_ms()
    return [key for key, when in list(_resume_schedule.items()) if when <= now]


def _reschedule_resume(scope_key: str, delay_ms: int) -> None:
    _resume_schedule[scope_key] = now_ms() + delay_ms


def _drop_resume(scope_key: str) -> None:
    _resume_schedule.pop(scope_key, None)
    _resume_attempts.pop(scope_key, None)


async def respawn_after_restart(project: str, scope: str, *,
                                cli_session_id: str | None = None,
                                primer_text: str | None = None
                                ) -> AgentInstance:
    """Spawn a fresh AgentInstance for a scope whose previous handle is gone."""
    scope_key = _scope_key(project, scope)
    if _by_scope.get(scope_key) is not None:
        return _by_scope[scope_key]
    if primer_text:
        session = await create_session(
            project=project, scope=scope,
            agent_cli="claude",
            permission_mode="bypassPermissions",
            resume_session_id=None,
            fresh=True,
        )
        await send_slash(scope_key, primer_text)
        return session
    return await create_session(
        project=project, scope=scope,
        agent_cli="claude",
        permission_mode="bypassPermissions",
        resume_session_id=cli_session_id,
        fresh=False,
    )


async def _watch_resume_health(session: AgentInstance) -> bool:
    try:
        await asyncio.wait_for(
            session.proc.wait(), timeout=RESUME_HEALTH_WINDOW_S
        )
    except asyncio.TimeoutError:
        return True
    return (session.exit_code or 0) == 0


async def _drive_one_resume(scope_key: str, semaphore: asyncio.Semaphore) -> None:
    async with semaphore:
        attempts = _resume_attempts.get(scope_key, 0) + 1
        _resume_attempts[scope_key] = attempts
        if "/" not in scope_key:
            _drop_resume(scope_key)
            return
        project, scope = scope_key.split("/", 1)
        row = _get_dao().get_latest_instance_for_scope(project, scope)
        if row is None:
            _drop_resume(scope_key)
            return
        cli_session_id = row.get("cli_session_id")
        if attempts > RESUME_MAX_ATTEMPTS:
            cli_session_id = None

        try:
            session = await respawn_after_restart(
                project, scope, cli_session_id=cli_session_id)
        except Exception:  # noqa: BLE001
            if attempts >= RESUME_MAX_ATTEMPTS:
                _drop_resume(scope_key)
                return
            _reschedule_resume(scope_key, int(min(60_000 * (2 ** attempts), 600_000)))
            return

        healthy = await _watch_resume_health(session)
        if not healthy:
            if attempts >= RESUME_MAX_ATTEMPTS:
                _drop_resume(scope_key)
                return
            _reschedule_resume(scope_key, int(min(60_000 * (2 ** attempts), 600_000)))
            return

        _drop_resume(scope_key)


async def _drive_resume_queue() -> None:
    semaphore = asyncio.Semaphore(MAX_CONCURRENT_RESUMES)
    while True:
        due = _due_resume_scope_keys()
        if due:
            tasks = [
                asyncio.create_task(_drive_one_resume(key, semaphore))
                for key in due
            ]
            await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.sleep(RESUME_DRIVER_POLL_S)


def start_resume_driver() -> asyncio.Task:
    global _resume_driver_task
    if _resume_driver_task is not None and not _resume_driver_task.done():
        return _resume_driver_task
    _resume_driver_task = asyncio.create_task(_drive_resume_queue())
    return _resume_driver_task


# ---------------------------------------------------------------------------
# Rooms-service dispatcher wiring — NO-OP in modular mode
# ---------------------------------------------------------------------------

def _dispatch_local_post(room_id: str, scope_key: str,
                         post_author: str, post_body: str) -> None:
    """Forward a room post to the in-memory session's input queue."""
    session = _by_scope.get(scope_key)
    if session is None:
        return
    enqueue_input(session, room_id, post_author, post_body)


def install_room_dispatchers() -> None:
    """No-op in modular mode.

    In the monolith, this wired rooms_svc callbacks. In the modular world,
    room posts arrive via gatewayclient RPC (the scopes service routes them
    here via the agent's registered RPC handler). The hub_adapter wires
    those handlers on startup.
    """
    pass
