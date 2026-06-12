"""Agent instance management — rooms-aware, tracked, addressable (v37).

An AgentInstance owns one ``claude --input-format=stream-json
--output-format=stream-json`` (or ``opencode run --format json``) subprocess
attached to a single ``(project, scope)``. Its job is the *agent runtime*:
serialize inputs from any number of rooms into stdin, parse stdout, and
broadcast text/tool events into the agent's owned room transcript.

v37 model changes
-----------------

- **Identity** lives on the ``agents`` row (uuid PK, ``status`` ∈
  ``allocated|active|retired``). Each spawn opens a new ``agent_instances``
  row carrying the CLI-specific session token (``cli_session_id``) and
  per-spawn lifecycle (``started_at``/``ended_at``/``data`` JSON). Resume
  reads the latest ``agent_instances`` row for that agent.
- **Transcript** writes go through ``agent_transcript`` which forwards
  into the agent's owned room's ``room_transcripts``.
- **Resume scheduling** is process-local: a ``dict[agent_id → wake_at_ms]``
  plus the simple "every ``agents`` row with ``status='active'`` and no
  live in-memory handle gets respawned" rule. No more
  ``agent_resume_queue`` table.

The rooms service registers ``_local_scope_dispatcher`` against this
module so it can push posts into ``session.input_queue`` directly.
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
from awm.db import get_connection
from awm.models import AgentSessionInfo
from awm.services import agent_transcript, identity
from awm.services import rooms as rooms_svc
from awm.services._path import resolve_bin
from awm.services.identity import iso_to_ms, ms_to_iso, now_ms


_SUPPORTED_CLIS = {"claude", "opencode"}
_INPUT_QUEUE_SIZE = 128


def _scope_key(project: str, scope: str) -> str:
    return f"{project}/{scope}"


# ---------------------------------------------------------------------------
# AgentInstance
# ---------------------------------------------------------------------------

class AgentInstance:
    """In-memory handle for a running CLI subprocess.

    ``id`` is the ``agent_instances.id`` (per-spawn integer); ``agent_id``
    is the long-lived ``agents.id`` (uuid). Other fields mirror v36 for
    minimal disruption to the input pump / reader loop / compact dance.
    """

    def __init__(
        self,
        *,
        id: int,
        agent_id: str,
        project: str,
        scope: str,
        agent_cli: str,
        log_path: Path,
        proc: asyncio.subprocess.Process,
    ):
        self.id = id
        self.agent_id = agent_id
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
        self.input_queue: asyncio.Queue[tuple[str, rooms_svc.Post]] = (
            asyncio.Queue(maxsize=_INPUT_QUEUE_SIZE)
        )
        self.reader_task: Optional[asyncio.Task] = None
        self.waiter_task: Optional[asyncio.Task] = None
        self.input_pump_task: Optional[asyncio.Task] = None
        self.stdin_ready: asyncio.Event = asyncio.Event()
        self.stdin_frames_log = log_path.parent / "agent.log"
        # Spawn-time flags + captured CLI session id (from init event).
        self.permission_mode: str = "default"
        self.model: Optional[str] = None
        self.effort: Optional[str] = None
        # cli_session_id — claude session id today; the resume-driver
        # passes ``--resume <id>`` (claude) or the opencode equivalent.
        self.cli_session_id: Optional[str] = None
        self.claude_slash_commands: list[str] = []
        self.context_used: int = 0
        self.context_max: Optional[int] = None
        self.respawn_lock: asyncio.Lock = asyncio.Lock()
        self.compacting: bool = False

    # Legacy alias for code paths that still read .claude_session_id.
    @property
    def claude_session_id(self) -> Optional[str]:
        return self.cli_session_id

    @claude_session_id.setter
    def claude_session_id(self, value: Optional[str]) -> None:
        self.cli_session_id = value


# In-memory registries.
_registry_by_id: dict[int, AgentInstance] = {}
_by_agent_id: dict[str, AgentInstance] = {}
_by_scope: dict[str, AgentInstance] = {}
_registry_lock = asyncio.Lock()

# Resume wake schedule — agent_id → unix-ms. Populated at restart by
# reconcile_on_startup; drained by the resume driver.
_resume_schedule: dict[str, int] = {}


class ScopeBusyError(Exception):
    """A second AgentInstance spawn for an already-running scope was attempted."""


class NoSessionError(Exception):
    """No agent instance exists for the requested scope."""


# ---------------------------------------------------------------------------
# DB helpers (v37 agent_instances + agents)
# ---------------------------------------------------------------------------

def _ensure_agent_row(*, project: str, scope: str, agent_cli: str,
                      worktree: Path) -> str:
    """Get-or-create the agents row for ``(project, scope)``. Bumps status
    to ``active`` and returns the agent id."""
    conn = get_connection()
    try:
        pid = identity.project_id_for_name(project, conn=conn)
        if pid is None:
            # First-touch agent for an unseeded project — synthesize a
            # projects row from the canonical workspace layout.
            bare = PROJECTS_DIR / project / ".bare"
            identity.ensure_project(project, repo_path=str(bare), conn=conn)
        agent_id = identity.ensure_agent(
            project, scope,
            branch=f"feat/{scope}",
            worktree=str(worktree),
            agent_cli=agent_cli,
            status="active",
            conn=conn,
        )
        # ensure_agent uses 'allocated' for fresh rows — bump to 'active'
        # for live spawns. Also clear any stale retired_at.
        conn.execute(
            "UPDATE agents SET status='active', retired_at=NULL, "
            "agent_cli=?, worktree=? WHERE id=?",
            (agent_cli, str(worktree), agent_id),
        )
        conn.commit()
        return agent_id
    finally:
        conn.close()


def _open_agent_instance(*, agent_id: str, log_path: Path,
                         cli_session_id: str | None,
                         intent: str = "live") -> int:
    """Insert a new agent_instances row (ended_at=NULL). Returns the row id."""
    data = json.dumps({"intent": intent}, sort_keys=True)
    conn = get_connection()
    try:
        cur = conn.execute(
            "INSERT INTO agent_instances "
            "(agent_id, cli_session_id, log_path, started_at, ended_at, data) "
            "VALUES (?, ?, ?, ?, NULL, ?)",
            (agent_id, cli_session_id, str(log_path), now_ms(), data),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def _close_agent_instance(instance_id: int, *, exit_code: int | None,
                          intent_override: str | None = None) -> None:
    """Close an agent_instances row (set ended_at + merge into data JSON)."""
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT data FROM agent_instances WHERE id=?", (instance_id,),
        ).fetchone()
        try:
            data = json.loads(row[0]) if row and row[0] else {}
        except (TypeError, ValueError):
            data = {}
        if intent_override is not None:
            data["intent"] = intent_override
        data["exit_code"] = exit_code
        conn.execute(
            "UPDATE agent_instances SET ended_at=?, data=? WHERE id=?",
            (now_ms(), json.dumps(data, sort_keys=True), instance_id),
        )
        conn.commit()
    finally:
        conn.close()


def _set_instance_intent(instance_id: int, intent: str) -> None:
    """Persist why an instance is about to exit (live/stopped/killed/compacted)."""
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT data FROM agent_instances WHERE id=?", (instance_id,),
        ).fetchone()
        try:
            data = json.loads(row[0]) if row and row[0] else {}
        except (TypeError, ValueError):
            data = {}
        data["intent"] = intent
        conn.execute(
            "UPDATE agent_instances SET data=? WHERE id=?",
            (json.dumps(data, sort_keys=True), instance_id),
        )
        conn.commit()
    finally:
        conn.close()


def _update_instance_cli_session_id(instance_id: int, cli_sid: str) -> None:
    conn = get_connection()
    try:
        conn.execute(
            "UPDATE agent_instances SET cli_session_id=? WHERE id=?",
            (cli_sid, instance_id),
        )
        conn.commit()
    finally:
        conn.close()


def _ensure_owned_room(*, agent_id: str, project: str, scope: str) -> str:
    """Get-or-create the agent's owned room. Used by the transcript
    writer + the post-restart notice path."""
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT id FROM rooms WHERE owner_agent_id=? AND status='open' "
            "ORDER BY created_at DESC LIMIT 1",
            (agent_id,),
        ).fetchone()
    finally:
        conn.close()
    if row:
        return row[0]
    # Create one via rooms_svc.
    room = rooms_svc.create_room(
        topic=f"{project}/{scope} agent session",
        owner_agent_id=agent_id,
        opener=f"scope:{project}/{scope}",
    )
    return room.id


# ---------------------------------------------------------------------------
# Public API: create / lookup
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


async def create_session(*, project: str, scope: str,
                         agent_cli: str = "claude",
                         permission_mode: str = "default",
                         model: Optional[str] = None,
                         effort: Optional[str] = None,
                         resume_session_id: Optional[str] = None,
                         fresh: bool = False) -> AgentInstance:
    """Spawn a CLI subprocess and register it. Raises ScopeBusyError if
    the scope already has a live in-memory handle.

    ``fresh=True`` skips the DB-recovered resume id (used by /clear).
    """
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

        agent_id = _ensure_agent_row(
            project=project, scope=scope, agent_cli=agent_cli,
            worktree=workspace_dir,
        )

        # Resume id recovery: if not passed and not ``fresh``, look at the
        # most recent agent_instances row for this agent.
        if resume_session_id is None and not fresh:
            conn = get_connection()
            try:
                row = conn.execute(
                    "SELECT cli_session_id FROM agent_instances "
                    "WHERE agent_id=? AND cli_session_id IS NOT NULL "
                    "ORDER BY started_at DESC LIMIT 1",
                    (agent_id,),
                ).fetchone()
                if row is not None:
                    resume_session_id = row[0]
            finally:
                conn.close()

        instance_id = _open_agent_instance(
            agent_id=agent_id, log_path=log_path,
            cli_session_id=resume_session_id,
        )

        # Ensure the owned room exists before we spawn the subprocess —
        # the reader loop's transcript writes assume the room is there.
        try:
            _ensure_owned_room(agent_id=agent_id, project=project, scope=scope)
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
            _close_agent_instance(instance_id, exit_code=-1,
                                  intent_override="failed_to_spawn")
            raise RuntimeError(f"{agent_cli} binary not on PATH: {exc}") from exc
        finally:
            log_fp.close()

        session = AgentInstance(
            id=instance_id,
            agent_id=agent_id,
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
        _by_agent_id[agent_id] = session
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
            _by_agent_id.pop(agent_id, None)
            _registry_by_id.pop(instance_id, None)
        _close_agent_instance(instance_id, exit_code=-1,
                              intent_override="stdin_ready_timeout")
        raise RuntimeError(
            f"input pump never signaled stdin-ready for {key} within 10s"
        )
    return session


def get_session_by_scope(project: str, scope: str) -> AgentInstance | None:
    return _by_scope.get(_scope_key(project, scope))


def get_session(session_id: int) -> AgentInstance | None:
    return _registry_by_id.get(session_id)


def get_session_by_agent_id(agent_id: str) -> AgentInstance | None:
    return _by_agent_id.get(agent_id)


# ---------------------------------------------------------------------------
# Input pump — serialize per-room posts into stdin
# ---------------------------------------------------------------------------

async def _input_pump(session: AgentInstance) -> None:
    session.stdin_ready.set()
    if (session.proc is None or session.proc.stdin is None
            or session.proc.stdin.is_closing()):
        return
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
        try:
            with session.stdin_frames_log.open("a", encoding="utf-8") as fp:
                fp.write(f"STDIN {framed_body!r}\n")
        except OSError:
            pass
        agent_transcript.record_in(session, framed_body, injection=False)


def enqueue_input(session: AgentInstance, room_id: str, post: rooms_svc.Post) -> bool:
    try:
        session.input_queue.put_nowait((room_id, post))
        return True
    except asyncio.QueueFull:
        return False


# ---------------------------------------------------------------------------
# Output reader — parse stream-json, broadcast to rooms
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


def _rooms_for_agent(agent_id: str) -> list[str]:
    """Active locally-hosted rooms involving ``agent_id`` — either as
    owner or as an agent-kind guest. Used to broadcast agent output."""
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT DISTINCT r.id FROM rooms r "
            "LEFT JOIN guest_list g ON g.room_id = r.id "
            "WHERE r.status='open' AND ("
            "  r.owner_agent_id = ? "
            "  OR (g.guest_kind='agent' AND g.guest_ref = ?)"
            ")",
            (agent_id, agent_id),
        ).fetchall()
    finally:
        conn.close()
    return [r[0] for r in rows]


def _rooms_for_scope(scope_key: str) -> list[str]:
    """Legacy adapter — convert scope_key to agent_id then dispatch."""
    if "/" not in scope_key:
        return []
    project, scope = scope_key.split("/", 1)
    agent_id = identity.agent_id_for_scope(project, scope, active_only=False)
    if not agent_id:
        return []
    return _rooms_for_agent(agent_id)


async def _reader_loop(session: AgentInstance) -> None:
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
                _update_instance_cli_session_id(session.id, sid)
            cmds = parsed.get("slash_commands")
            if isinstance(cmds, list):
                session.claude_slash_commands = [c for c in cmds if isinstance(c, str)]
            init_model = parsed.get("model") or session.model
            session.context_max = _lookup_context_max(init_model)

        _update_usage_from_event(session, parsed)

        events = _extract_renderable(parsed)
        if not events:
            continue

        rooms = _rooms_for_agent(session.agent_id)
        if not rooms:
            continue
        author_ref = session.agent_id
        for kind, body in events:
            for room_id in rooms:
                try:
                    rooms_svc.post_transcript(
                        room_id, author=author_ref, body=body, kind=kind,
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
    session.exited_at_ms = now_ms()
    final = "killed" if session.status == "killed" else "exited"
    session.status = final

    _close_agent_instance(session.id, exit_code=exit_code)

    if session.input_pump_task is not None:
        session.input_pump_task.cancel()

    async with _registry_lock:
        if _by_scope.get(session.scope_key) is session:
            _by_scope.pop(session.scope_key, None)
        if _by_agent_id.get(session.agent_id) is session:
            _by_agent_id.pop(session.agent_id, None)
        _registry_by_id.pop(session.id, None)

    try:
        rooms_svc.auto_close_for_scope(session.scope_key)
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# Stop / kill
# ---------------------------------------------------------------------------

def _info_for_instance_row(row) -> AgentSessionInfo:
    return AgentSessionInfo(
        id=row["id"],
        project=row["project_name"],
        scope=row["scope"],
        pid=row["pid"] or 0,
        status=row["render_status"],
        agent_cli=row["agent_cli"],
        started_at=ms_to_iso(row["started_at"]) or "",
        exited_at=ms_to_iso(row["ended_at"]),
        exit_code=row["exit_code"],
        attached=False,
    )


def _render_status(row) -> str:
    if row["ended_at"] is None:
        # Still open per DB. If in-memory handle exists → 'running'; else
        # 'orphaned' (process gone but row not closed yet — reconciliation
        # will tidy it).
        return "running" if row["id"] in _registry_by_id else "orphaned"
    try:
        data = json.loads(row["data"] or "{}")
    except (TypeError, ValueError):
        data = {}
    intent = data.get("intent") or "live"
    if intent == "killed":
        return "killed"
    if intent in ("stopped", "compacted"):
        return "exited"
    return "exited"


def _hydrate_instance_row(row: dict) -> dict:
    """Augment an agent_instances+agents row with the synthesized
    ``project_name``/``pid``/``exit_code``/``render_status`` fields the
    AgentSessionInfo adapter needs."""
    instance_handle = _registry_by_id.get(row["id"])
    pid = instance_handle.proc.pid if instance_handle else 0
    try:
        data = json.loads(row["data"] or "{}")
    except (TypeError, ValueError):
        data = {}
    out = dict(row)
    out["project_name"] = row["project_name"]
    out["pid"] = pid
    out["exit_code"] = data.get("exit_code")
    out["render_status"] = _render_status(out)
    return out


def _row_for_instance(instance_id: int) -> dict | None:
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT i.*, a.scope, a.agent_cli, p.name AS project_name "
            "FROM agent_instances i "
            "JOIN agents a ON a.id = i.agent_id "
            "JOIN projects p ON p.id = a.project_id "
            "WHERE i.id=?",
            (instance_id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    return _hydrate_instance_row(dict(row))


async def stop_session(session_id: int) -> AgentSessionInfo:
    session = _registry_by_id.get(session_id)
    row = _row_for_instance(session_id)
    if row is None:
        raise FileNotFoundError(f"session {session_id} not found")
    if row["ended_at"] is not None:
        return _info_for_instance_row(row)
    _set_instance_intent(session_id, "stopped")
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
    if row["ended_at"] is not None:
        return _info_for_instance_row(row)
    _set_instance_intent(session_id, "killed")
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
    agent_transcript.record_in(session, body, injection=True)


# ---------------------------------------------------------------------------
# Synthetic /compact
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
    if agent_transcript.has_unmatched_tool_use(session.agent_id):
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
            summary = agent_transcript.read_recent_assistant_text(session.agent_id)
        if not summary or len(summary) < COMPACT_MIN_SUMMARY_CHARS:
            return (
                f"compact failed: no usable summary captured "
                f"(got {len(summary)} chars); session untouched"
            )

        _set_instance_intent(prior_instance_id, "compacted")

        try:
            new_session = await respawn_session(scope_key, clear_history=True)
        except Exception as exc:  # noqa: BLE001
            return (
                f"compact respawn failed: {exc}; previous-session summary "
                f"available via room transcript (len {len(summary)})"
            )

        primer = f"[system: previous-session-summary]\n{summary}"
        try:
            await send_slash(scope_key, primer)
        except NoSessionError as exc:
            return (
                f"compact primer injection failed: {exc}; session "
                f"summary still available via transcript"
            )

        # Audit notice
        for room_id in _rooms_for_agent(new_session.agent_id):
            try:
                rooms_svc.post_transcript(
                    room_id, author=identity.SYSTEM_REF,
                    body=f"[compact summary for {scope_key}]\n{summary}",
                    kind="compact",
                )
            except rooms_svc.RoomError:
                continue

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
    """Render an AgentSessionInfo per agent_instances row, joined to its
    agent + project for the legacy project/scope display."""
    where = []
    params: list = []
    if project:
        where.append("p.name = ?")
        params.append(project)
    if scope:
        where.append("a.scope = ?")
        params.append(scope)
    sql = (
        "SELECT i.*, a.scope, a.agent_cli, p.name AS project_name "
        "FROM agent_instances i "
        "JOIN agents a ON a.id = i.agent_id "
        "JOIN projects p ON p.id = a.project_id"
    )
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY i.id DESC"
    conn = get_connection()
    try:
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()
    out: list[AgentSessionInfo] = []
    for r in rows:
        hydrated = _hydrate_instance_row(dict(r))
        if status and hydrated["render_status"] != status:
            continue
        out.append(_info_for_instance_row(hydrated))
    return out


def tail_log(session_id: int, lines: int = 200) -> str:
    row = _row_for_instance(session_id)
    if row is None:
        raise FileNotFoundError(f"session {session_id} not found")
    path = Path(row["log_path"] or "")
    if not path or not path.exists():
        return ""
    with path.open("r", encoding="utf-8", errors="replace") as fp:
        data = fp.readlines()
    return "".join(data[-lines:])


def scrub_resume_queue_for_scope(project: str, scope: str) -> int:
    """v37 no-op compatibility shim. Resume scheduling is in-memory only;
    /kill + /clear paths can call this freely without persistence side
    effects. Returns 1 if an in-memory wake was cleared, else 0."""
    agent_id = identity.agent_id_for_scope(project, scope, active_only=False)
    if not agent_id:
        return 0
    return 1 if _resume_schedule.pop(agent_id, None) is not None else 0


# ---------------------------------------------------------------------------
# Reconciliation on startup
# ---------------------------------------------------------------------------

def reconcile_on_startup() -> None:
    """After a server restart: close any open agent_instances rows whose
    PID is gone (server-PID continuity broken by definition), retire
    vagrant agents (ephemeral), and seed the resume schedule for every
    ``agents`` row with ``status='active'`` so the resume driver brings
    them back.
    """
    conn = get_connection()
    try:
        # Close open agent_instances rows — daemon restart breaks
        # continuity by definition.
        open_rows = conn.execute(
            "SELECT i.id, i.data FROM agent_instances i WHERE i.ended_at IS NULL"
        ).fetchall()
        for r in open_rows:
            try:
                data = json.loads(r[1] or "{}")
            except (TypeError, ValueError):
                data = {}
            data["closed_by"] = "reconcile"
            data["reason"] = "daemon_restart"
            conn.execute(
                "UPDATE agent_instances SET ended_at=?, data=? WHERE id=?",
                (now_ms(), json.dumps(data, sort_keys=True), r[0]),
            )

        # Retire vagrant agents — ephemeral.
        conn.execute(
            "UPDATE agents SET status='retired', retired_at=? "
            "WHERE is_vagrant=1 AND status IN ('allocated','active')",
            (now_ms(),),
        )

        # Schedule resume for every active agent.
        active = conn.execute(
            "SELECT id FROM agents WHERE status='active'",
        ).fetchall()
        for r in active:
            _resume_schedule[r[0]] = now_ms()
        conn.commit()
    finally:
        conn.close()


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


def _due_resume_agent_ids() -> list[str]:
    now = now_ms()
    return [aid for aid, when in list(_resume_schedule.items()) if when <= now]


def _reschedule_resume(agent_id: str, delay_ms: int) -> None:
    _resume_schedule[agent_id] = now_ms() + delay_ms


def _drop_resume(agent_id: str) -> None:
    _resume_schedule.pop(agent_id, None)
    _resume_attempts.pop(agent_id, None)


def _agent_row_for_resume(agent_id: str) -> dict | None:
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT a.id, a.agent_cli, p.name AS project_name, a.scope, "
            "       i.cli_session_id, i.ended_at, i.started_at "
            "FROM agents a "
            "JOIN projects p ON p.id = a.project_id "
            "LEFT JOIN agent_instances i ON i.agent_id = a.id "
            "WHERE a.id=? "
            "ORDER BY i.started_at DESC LIMIT 1",
            (agent_id,),
        ).fetchone()
    finally:
        conn.close()
    return dict(row) if row else None


async def respawn_after_restart(agent_row: dict, *,
                                primer_text: str | None = None
                                ) -> AgentInstance:
    """Spawn a fresh AgentInstance for an agent whose previous in-memory
    handle is gone (daemon restart). The CLI driver is named by
    ``agents.agent_cli``; the resume token comes from the latest
    ``agent_instances.cli_session_id``."""
    project = agent_row["project_name"]
    scope = agent_row["scope"]
    scope_key = _scope_key(project, scope)
    if _by_scope.get(scope_key) is not None:
        return _by_scope[scope_key]
    if primer_text:
        session = await create_session(
            project=project, scope=scope,
            agent_cli=agent_row["agent_cli"] or "claude",
            permission_mode="bypassPermissions",
            resume_session_id=None,
            fresh=True,
        )
        await send_slash(scope_key, primer_text)
        return session
    return await create_session(
        project=project, scope=scope,
        agent_cli=agent_row["agent_cli"] or "claude",
        permission_mode="bypassPermissions",
        resume_session_id=agent_row.get("cli_session_id"),
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


async def _drive_one_resume(agent_id: str, semaphore: asyncio.Semaphore) -> None:
    async with semaphore:
        attempts = _resume_attempts.get(agent_id, 0) + 1
        _resume_attempts[agent_id] = attempts
        row = _agent_row_for_resume(agent_id)
        if row is None:
            _drop_resume(agent_id)
            return
        if attempts > RESUME_MAX_ATTEMPTS:
            # Last-resort fallback: clear the cli session id so we spawn fresh.
            row["cli_session_id"] = None

        try:
            session = await respawn_after_restart(row)
        except Exception:  # noqa: BLE001
            if attempts >= RESUME_MAX_ATTEMPTS:
                _drop_resume(agent_id)
                return
            _reschedule_resume(agent_id, int(min(60_000 * (2 ** attempts), 600_000)))
            return

        healthy = await _watch_resume_health(session)
        if not healthy:
            if attempts >= RESUME_MAX_ATTEMPTS:
                _drop_resume(agent_id)
                return
            _reschedule_resume(agent_id, int(min(60_000 * (2 ** attempts), 600_000)))
            return

        _drop_resume(agent_id)


async def _drive_resume_queue() -> None:
    semaphore = asyncio.Semaphore(MAX_CONCURRENT_RESUMES)
    while True:
        due = _due_resume_agent_ids()
        if due:
            tasks = [
                asyncio.create_task(_drive_one_resume(aid, semaphore))
                for aid in due
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
# Rooms-service dispatcher wiring
# ---------------------------------------------------------------------------

def _dispatch_local_post(room_id: str, scope_key: str,
                         post: rooms_svc.Post) -> None:
    session = _by_scope.get(scope_key)
    if session is None:
        return
    enqueue_input(session, room_id, post)


def _on_close_room_kill(room_id: str) -> None:
    """SIGTERM every locally-running agent participant of ``room_id``."""
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT a.id, p.name, a.scope "
            "FROM rooms r "
            "JOIN agents a ON a.id = r.owner_agent_id "
            "JOIN projects p ON p.id = a.project_id "
            "WHERE r.id=?",
            (room_id,),
        ).fetchall()
        guest_rows = conn.execute(
            "SELECT a.id, p.name, a.scope "
            "FROM guest_list g "
            "JOIN agents a ON a.id = g.guest_ref AND g.guest_kind='agent' "
            "JOIN projects p ON p.id = a.project_id "
            "WHERE g.room_id=?",
            (room_id,),
        ).fetchall()
    finally:
        conn.close()
    for r in list(rows) + list(guest_rows):
        scope_key = f"{r['name']}/{r['scope']}"
        session = _by_scope.get(scope_key)
        if session is None:
            continue
        try:
            session.proc.terminate()
        except ProcessLookupError:
            pass
        session.status = "stopping"
        _set_instance_intent(session.id, "stopped")


def install_room_dispatchers() -> None:
    rooms_svc.set_local_scope_dispatcher(_dispatch_local_post)
    rooms_svc.set_close_room_kill_callback(_on_close_room_kill)


install_room_dispatchers()
