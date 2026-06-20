"""Agent instance management — tracked, addressable (modular v1).

An AgentInstance owns one ``claude`` or ``opencode`` subprocess attached to a
single ``(project, scope)``. Its job is the *agent runtime*: serialize inputs
into stdin, parse stdout, and post rendered text/tool events to the agent's
**scope channel**.

Subscribe to agents, message scopes
-----------------------------------
Raw agent acts (the full structured stdout event stream) belong to the *agent*
and stay local in ``agents.db`` (``agent_transcript``) — you subscribe to an
*agent* for those. The agent's rendered, human-facing output is also posted to
its *scope channel* (``scope_post`` on the scopes service) — a scope IS the
channel, addressed by ``(project, scope)``; there are no rooms.

Modular changes from the monolith
----------------------------------
- **Identity** is resolved via gatewayclient calls to the ``scopes`` service.
  No uuid ``agent_id``; ``(project, scope)`` is the natural key throughout.
- **Persistence** goes through ``AgentsDAO`` → ``agents.db``; no shared
  ``state.db``.
- **Channel output** is cross-service: ``scope_post`` on the ``scopes``
  service. The scope's channel exists with the scope (``ensureScope``), so
  there is nothing to create or auto-close.
- **Raw act writes** go to the ``agent_transcript`` table in ``agents.db``
  via ``agent_transcript.record_*`` (which uses AgentsDAO).
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

from awm.agentcore import AgentConfig, open_agent
from awm.agentcore.session import AgentSession as _CoreSession


_SUPPORTED_CLIS = {"claude", "opencode"}
_INPUT_QUEUE_SIZE = 128

# Supervision (T4): a task-bound worker gets a hard turn budget — every turn
# boundary decrements it, with NO extension and NO refill. The final stretch
# escalates to a warning so the worker checkpoints + self-fails gracefully.
# These live here (not placement.py) because AgentInstance.__init__ seeds the
# per-session counter; placement.py reads them back for the driver.
TASK_TURN_BUDGET = 100
TASK_WARN_REMAINING = 15

# Scope→stdin delivery rides a LIVE subscription to the scopes `posts` emitter
# (not a poll). On each (re)connect a single cursored `scope_fetch` closes the
# gap for anything posted while we were disconnected; this caps that read.
_SCOPE_RESYNC_BATCH = 50

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

    The subprocess + stream parsing are owned by an agentcore
    :class:`AgentSession` (``agent_session``); this class keeps the supervisor
    concerns (registry, transcript, scope-attach, resume, slash/compact). The
    ``proc`` property surfaces the agentcore session's underlying process so the
    existing stop/kill/slash/pid paths keep working unchanged.
    """

    def __init__(
        self,
        *,
        id: int,
        project: str,
        scope: str,
        agent_cli: str,
        log_path: Path,
        agent_session: _CoreSession,
    ):
        self.id = id
        self.project = project
        self.scope = scope
        self.scope_key = _scope_key(project, scope)
        self.agent_cli = agent_cli
        self.log_path = log_path
        self.agent_session = agent_session
        self.status: str = "running"
        self.started_at_ms: int = now_ms()
        self.exited_at_ms: Optional[int] = None
        self.exit_code: Optional[int] = None
        self.input_queue: asyncio.Queue = asyncio.Queue(maxsize=_INPUT_QUEUE_SIZE)
        self.reader_task: Optional[asyncio.Task] = None
        self.waiter_task: Optional[asyncio.Task] = None
        self.input_pump_task: Optional[asyncio.Task] = None
        self.scope_poll_task: Optional[asyncio.Task] = None
        # Cursor for the scope→stdin poller (ISO ts of the last post delivered).
        self.scope_poll_after_ts: Optional[str] = None
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
        # Task-bounded placement. For a conversational session these stay at
        # their defaults and every task path short-circuits. For a worker the
        # placement IS this instance row; placement_token names it, agent_ref is
        # the stable placement identity, task_ref binds it to the task.
        self.mode: str = "conversational"
        self.task_ref: Optional[str] = None
        self.agent_ref: Optional[str] = None
        self.placement_token: Optional[str] = None
        # Placement workdir (the workspace unit path) + the per-mode tool
        # profile; both carried across respawn. Default-empty for conversational.
        self.workdir: Optional[str] = None
        self.allowed_tools: Optional[list[str]] = None
        # Supervision: a hard turn budget that decrements every turn boundary,
        # no extension, no refill (only meaningful when mode != conversational).
        self.turn_budget: int = TASK_TURN_BUDGET
        # Idempotent streaming: message_id → accumulated text. Partials stream
        # live-only (bus, no persist); the finalized message is upserted as one
        # durable row keyed by message_id. Both reset per turn (on `result`) so
        # a long-lived session doesn't leak ids.
        self._partial_accum: dict[str, str] = {}
        self._msg_accum: dict[str, str] = {}

    @property
    def proc(self):
        """The agentcore session's underlying subprocess (``_proc``).

        Surfaced so the supervisor's stop/kill/slash/pid code keeps reaching
        the real process without knowing about agentcore internals."""
        return getattr(self.agent_session, "_proc", None)

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
# Scope channel output (a scope IS the channel; post the agent's rendered
# output to its own scope channel via the scopes service)
# ---------------------------------------------------------------------------

async def _post_to_scope(*, project: str, scope: str, author: str,
                         body: str, kind: str) -> None:
    """Post one rendered agent event to the agent's scope channel."""
    try:
        await gatewayclient.call('scopes', 'scope_post', {
            'project': project, 'scope': scope,
            'author': author, 'body': body, 'kind': kind,
        })
    except Exception:  # noqa: BLE001
        pass


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
# agentcore config builder
# ---------------------------------------------------------------------------

def _write_placement_mcp_config(awm_dir: Path, as_: str) -> Optional[str]:
    """Write a per-placement ``spawn-mcp.json`` carrying the agent's identity.

    A placed agent submits work through MCP B-op tools (``edit_deliverable`` …)
    that must resolve to ITS placement without the model supplying a token. We
    clone the canonical ``spawn-mcp.json`` into the placement's own unit
    (``<unit>/.awm/spawn-mcp.json``) and inject ``mcpServers.awm.env.AWM_AS =
    f"{project}/{unit_slug}"``. Each placed ``claude`` spawns its OWN ``awm-mcp``
    stdio child from this file, so that proxy stamps ``X-Awm-As`` on every
    ``/invoke`` — the agents service resolves the open placement from it. Returns
    the per-placement config path, or None to fall back to the canonical file
    (no identity → the relays reject the call, which is the safe failure)."""
    canonical = config.AWM_DIR / "spawn-mcp.json"
    if not canonical.exists():
        return None
    try:
        cfg = json.loads(canonical.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    servers = cfg.setdefault("mcpServers", {})
    awm = servers.get("awm")
    if isinstance(awm, dict):
        env = awm.setdefault("env", {})
        if isinstance(env, dict):
            env["AWM_AS"] = as_
    dest = awm_dir / "spawn-mcp.json"
    try:
        dest.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    except OSError:
        return None
    return str(dest)


def _build_core_config(
    *, agent_cli: str, permission_mode: str, model: Optional[str],
    effort: Optional[str], resume_session_id: Optional[str],
    workspace_dir: Path, awm_dir: Path,
    allowed_tools: Optional[list[str]] = None,
    disallowed_tools: Optional[list[str]] = None,
    mcp_config_override: Optional[str] = None,
) -> AgentConfig:
    """Map the agents-service spawn args onto an agentcore :class:`AgentConfig`.

    ``permission_mode == 'bypassPermissions'`` → full-open (``permissions='full'``);
    everything else maps to ``permissions='default'`` (the harness's own
    default). ``effort`` rides ``params`` (claude). ``mcp_config`` is the
    workspace ``spawn-mcp.json`` for claude; opencode is configured via the
    ``OPENCODE_CONFIG`` env it inherits from this process, so it is not threaded
    through the config here. ``allowed_tools`` / ``disallowed_tools`` are the
    per-placement tool profile (a task-bound worker is full-open on permissions
    but its tools — fs built-ins + which worker MCP tools — are scoped by the
    allowlist; a conversational session passes neither and is unrestricted).
    """
    permissions = "full" if permission_mode == "bypassPermissions" else "default"
    params: dict = {}
    if effort:
        params["effort"] = effort
    mcp_config: Optional[str] = None
    if agent_cli == "claude":
        if mcp_config_override:
            mcp_config = mcp_config_override
        else:
            spawn_mcp = config.AWM_DIR / "spawn-mcp.json"
            if spawn_mcp.exists():
                mcp_config = str(spawn_mcp)
    return AgentConfig(
        harness=agent_cli,
        mode="live",
        model=model,
        params=params,
        permissions=permissions,
        workdir=str(workspace_dir),
        resume_id=resume_session_id,
        mcp_config=mcp_config,
        allowed_tools=allowed_tools,
        disallowed_tools=disallowed_tools,
    )


# ---------------------------------------------------------------------------
# Public API: create / lookup
# ---------------------------------------------------------------------------

async def create_session(*, project: str, scope: str,
                         agent_cli: str = "claude",
                         permission_mode: str = "default",
                         model: Optional[str] = None,
                         effort: Optional[str] = None,
                         resume_session_id: Optional[str] = None,
                         fresh: bool = False,
                         mode: str = "conversational",
                         task_ref: Optional[str] = None,
                         agent_ref: Optional[str] = None,
                         placement_token: Optional[str] = None,
                         workdir: Optional[str] = None,
                         allowed_tools: Optional[list[str]] = None,
                         disallowed_tools: Optional[list[str]] = None,
                         ) -> AgentInstance:
    """Spawn a CLI subprocess and register it.

    The ``mode``/``task_ref``/``agent_ref``/``placement_token`` params are the
    task-bounded placement extension; they default such that every existing
    conversational caller is unchanged. When ``mode != 'conversational'`` the
    session is a **placement**: its workdir is the workspace UNIT (``workdir``,
    required), NOT a git scope, so it does NOT touch the scopes service (no
    ensure-scope, no scope-channel delivery — a human attaches via the
    transcript WS and pushes text via ``agent_post``). ``allowed_tools`` /
    ``disallowed_tools`` carry the per-mode tool profile. The DAO insert records
    the placement row and the supervision driver takes over the lifecycle."""
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
    task_bound = mode != "conversational"
    if task_bound:
        # Placement: the workdir is the workspace UNIT (provisioned by the
        # workspace service), not a git scope under PROJECTS_DIR.
        if not workdir:
            raise ValueError("task-bound placement requires a workdir (unit path)")
        workspace_dir = Path(workdir)
    else:
        workspace_dir = PROJECTS_DIR / project / scope
    if not workspace_dir.exists():
        raise FileNotFoundError(f"Workspace dir not found at {workspace_dir}")
    awm_dir = workspace_dir / ".awm"
    awm_dir.mkdir(parents=True, exist_ok=True)
    log_path = awm_dir / "session.log"

    async with _registry_lock:
        if key in _by_scope:
            raise ScopeBusyError(
                f"scope {key} already has an active session "
                f"(pid={_by_scope[key].proc.pid if _by_scope[key].proc else '?'})"
            )

        # Conversational sessions live in a git scope (ensure it exists). A
        # placement runs in a workspace unit and never touches the scopes svc.
        if not task_bound:
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

        if mode == "conversational":
            instance_id = dao.open_instance(
                project=project, scope=scope,
                log_path=str(log_path),
                cli_session_id=resume_session_id,
                started_at=now_ms(),
            )
        else:
            instance_id = dao.open_task_instance(
                project=project, scope=scope,
                log_path=str(log_path),
                cli_session_id=resume_session_id,
                started_at=now_ms(),
                mode=mode,
                task_ref=task_ref,
                agent_ref=agent_ref,
                placement_token=placement_token,
            )

        # A scope IS the channel — it exists once the scope does (ensured
        # above); no separate room to provision.

        # Build the agentcore config and drive an AgentSession. The subprocess
        # + stream parsing live in agentcore now; we keep only the supervisor.
        # A placement gets a per-placement spawn-mcp config carrying its identity
        # (AWM_AS) so its B-op tools resolve to its own task without a token.
        mcp_config_override = None
        if task_bound and agent_cli == "claude":
            mcp_config_override = _write_placement_mcp_config(
                awm_dir, _scope_key(project, scope))
        core_config = _build_core_config(
            agent_cli=agent_cli, permission_mode=permission_mode,
            model=model, effort=effort, resume_session_id=resume_session_id,
            workspace_dir=workspace_dir, awm_dir=awm_dir,
            allowed_tools=allowed_tools, disallowed_tools=disallowed_tools,
            mcp_config_override=mcp_config_override,
        )
        agent_session = open_agent(core_config)

        # Subscribe BEFORE the first send so the claude init `status` event and
        # any early acts are captured (the pump-drained-past-us race).
        event_stream = agent_session.subscribe()
        try:
            await agent_session.start()
        except FileNotFoundError as exc:
            dao.close_instance(instance_id, ended_at=now_ms(), exit_code=-1,
                               intent_override="failed_to_spawn")
            raise RuntimeError(f"{agent_cli} binary not on PATH: {exc}") from exc
        except Exception as exc:  # noqa: BLE001
            dao.close_instance(instance_id, ended_at=now_ms(), exit_code=-1,
                               intent_override="failed_to_spawn")
            raise RuntimeError(
                f"failed to start {agent_cli} for {key}: {exc}") from exc

        session = AgentInstance(
            id=instance_id,
            project=project,
            scope=scope,
            agent_cli=agent_cli,
            log_path=log_path,
            agent_session=agent_session,
        )
        session.permission_mode = permission_mode
        session.model = model
        session.effort = effort
        session.cli_session_id = resume_session_id
        session.mode = mode
        session.task_ref = task_ref
        session.agent_ref = agent_ref
        session.placement_token = placement_token
        session.workdir = str(workspace_dir)
        session.allowed_tools = allowed_tools
        _registry_by_id[instance_id] = session
        _by_scope[key] = session

    session.reader_task = asyncio.create_task(_reader_loop(session, event_stream))
    session.waiter_task = asyncio.create_task(_waiter_loop(session))
    session.input_pump_task = asyncio.create_task(_input_pump(session))
    # Conversational sessions subscribe to the scope channel so human posts reach
    # stdin. A placement has no scope channel — a human attaches via the
    # transcript WS and pushes text via agent_post (direct enqueue), so skip it.
    if not task_bound:
        session.scope_poll_task = asyncio.create_task(_scope_delivery_loop(session))
    try:
        await asyncio.wait_for(session.stdin_ready.wait(), timeout=10.0)
    except asyncio.TimeoutError:
        proc = session.proc
        if proc is not None:
            try:
                proc.kill()
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
    """Drain the input queue into the agentcore session, one user turn each.

    The ``[from:author]`` framing (multi-party attribution) is preserved — it
    is the body the agent sees and what we record as the inbound transcript
    entry. ``enqueue_input`` stays the only path into this queue."""
    session.stdin_ready.set()
    while True:
        try:
            post_author, post_body = await session.input_queue.get()
        except asyncio.CancelledError:
            return
        framed_body = f"[from:{post_author}]\n{post_body}"
        try:
            await session.agent_session.send(framed_body)
        except (ConnectionResetError, BrokenPipeError):
            return
        except RuntimeError:
            # Session stdin not available (closing / not started) — stop pumping.
            return
        except Exception:  # noqa: BLE001
            return
        try:
            with session.stdin_frames_log.open("a", encoding="utf-8") as fp:
                fp.write(f"STDIN {framed_body!r}\n")
        except OSError:
            pass
        # record_in signature: (session, body, *, injection=False)
        from awm.agents import agent_transcript
        agent_transcript.record_in(session, framed_body, injection=False)


def enqueue_input(session: AgentInstance, post_author: str,
                  post_body: str) -> bool:
    """Enqueue a scope-channel post for the agent's stdin pump."""
    try:
        session.input_queue.put_nowait((post_author, post_body))
        return True
    except asyncio.QueueFull:
        return False


# ---------------------------------------------------------------------------
# Scope → stdin delivery (cross-service, live subscription)
# ---------------------------------------------------------------------------
# A scope IS the channel. Human `message`-kind posts to the scope must reach the
# agent's stdin. The scopes service emits a `posts` event on every new post, so
# the agents service SUBSCRIBES to it (no poll). On each (re)connect a single
# cursored `scope_fetch` closes the gap for anything posted while disconnected,
# then the live stream takes over. The agent's OWN posts (author
# `agent:proj/scope`) are ignored — agent output lives in the transcript, not
# re-delivered to itself. The cursor (ISO ts of the last delivered post) dedupes
# the resync/live overlap.

def _is_own_author(session: AgentInstance, author: str) -> bool:
    """True if `author` is this agent's own scope ref (don't self-deliver)."""
    if not author:
        return False
    a = author
    if a.startswith(("agent:", "scope:")):
        a = a.split(":", 1)[1]
    return a == f"{session.project}/{session.scope}"


def _maybe_deliver_post(session: AgentInstance, post: dict) -> None:
    """Route one scope post into the agent stdin if it's a fresh human message.

    Cursored on the ISO ts of the last delivered post so the resync/live overlap
    never double-delivers; only ``message``-kind posts not authored by the agent
    itself are enqueued."""
    if post.get('kind') != 'message':
        return
    ts = post.get('ts')
    ts_ms = iso_to_ms(ts)
    cursor_ms = iso_to_ms(session.scope_poll_after_ts)
    if cursor_ms is not None and ts_ms is not None and ts_ms <= cursor_ms:
        return
    if ts:
        session.scope_poll_after_ts = ts
    author = post.get('author') or ''
    if _is_own_author(session, author):
        return
    enqueue_input(session, author, post.get('body') or '')


async def _resync_scope_posts(session: AgentInstance) -> None:
    """One cursored `scope_fetch` to deliver anything posted during a gap.

    Run on each (re)connect before going live so a dropped WS never loses a
    human message. Best-effort: a transient RPC failure is swallowed (the live
    subscription still covers everything from here on)."""
    try:
        res = await gatewayclient.call('scopes', 'scope_fetch', {
            'project': session.project, 'scope': session.scope,
            'kind': 'message', 'limit': _SCOPE_RESYNC_BATCH, 'order': 'desc',
        })
    except Exception:  # noqa: BLE001
        return
    posts = (res or {}).get('posts') or []
    # Oldest→newest of the (newest-first) batch; the cursor filter dedupes.
    for post in reversed(posts):
        _maybe_deliver_post(session, post)


async def _scope_delivery_loop(session: AgentInstance) -> None:
    """Subscribe to the scopes `posts` emitter and route human posts to stdin.

    A live subscription (``gatewayclient.subscribe('scopes', 'posts')``), not a
    poll. Each (re)connect first runs a one-shot cursored resync to close the
    disconnect gap, then consumes live events filtered to this session's
    ``(project, scope)``. A clean WS close means "reconnect," not "done";
    reconnects use the same exponential backoff shape as the adapter."""
    # Seed the cursor at the newest existing post so we don't replay history
    # into stdin on attach (the transcript/backfill carries history already).
    try:
        recent = await gatewayclient.call('scopes', 'scope_fetch', {
            'project': session.project, 'scope': session.scope,
            'kind': 'message', 'limit': 1, 'order': 'desc',
        })
        posts = (recent or {}).get('posts') or []
        if posts:
            session.scope_poll_after_ts = posts[0].get('ts')
    except Exception:  # noqa: BLE001
        pass

    backoff = 1.0
    while True:
        try:
            # Close the gap for anything posted while we were disconnected,
            # then go live. The cursor dedupes the resync/live overlap.
            await _resync_scope_posts(session)
            async for ev in gatewayclient.subscribe('scopes', 'posts'):
                if not isinstance(ev, dict):
                    continue
                if (ev.get('project') != session.project
                        or ev.get('scope') != session.scope):
                    continue
                post = ev.get('post')
                if isinstance(post, dict):
                    _maybe_deliver_post(session, post)
            # Clean close → reconnect promptly.
            backoff = 1.0
        except asyncio.CancelledError:
            return
        except Exception:  # noqa: BLE001
            backoff = min(backoff * 2, 30.0)
        try:
            await asyncio.sleep(backoff)
        except asyncio.CancelledError:
            return


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


def _update_usage_from_data(session: "AgentInstance", data: dict | None) -> None:
    """Update context_used from an event's ``data['usage']`` block."""
    if not isinstance(data, dict):
        return
    usage = data.get("usage")
    if not isinstance(usage, dict):
        return
    try:
        inp = usage.get("input_tokens") or 0
        cache_read = usage.get("cache_read_input_tokens") or 0
        cache_create = usage.get("cache_creation_input_tokens") or 0
        total = int(inp) + int(cache_read) + int(cache_create)
        if total > 0:
            session.context_used = total
    except (TypeError, ValueError):
        return


def _message_id_for(event, data: dict) -> str:
    """The stable per-turn bubble key for a message/partial event.

    Prefer the harness ``message_id`` (claude stamps it; one bubble per
    assistant message). A backend that can't supply one (opencode) degrades to
    the event's own id — each act its own bubble: degraded, never wrong."""
    mid = data.get("message_id") if isinstance(data, dict) else None
    return mid if isinstance(mid, str) and mid else getattr(event, "id", "")


async def _reader_loop(session: AgentInstance, event_stream) -> None:
    """Consume agentcore :class:`AgentEvent`s: persist + fan out live.

    **Idempotent streaming.** A streamed reply is one logical message keyed by
    its harness ``message_id``:

    - ``partial`` events stream **live-only** — text is accumulated and a
      growing-bubble act is published over the bus, but nothing is persisted
      (this kills the durable-row flood + write amplification).
    - the finalized ``message`` event(s) **upsert** one durable row keyed by
      ``message_id`` (accumulating across a turn's text blocks).
    - ``result`` is still persisted (for its ``usage``/``session_id`` meta) but
      hidden downstream, and marks the turn boundary that resets the
      accumulators. ``status`` / ``tool_use`` / ``tool_result`` are unchanged.

    The agent's output is **not** posted back to its scope channel (transcript
    only) — deliberate agent messages (debrief) stay an explicit ``scope_post``
    elsewhere. The ``status`` (init) event carries the resolved harness session
    id (for resume) + slash commands + model; ``result``/``status`` carry usage.
    """
    from awm.agents import agent_transcript
    from awm.agents import agent_bus

    async for event in event_stream:
        data = getattr(event, "data", None) or {}
        kind = getattr(event, "kind", "status")

        # Lifecycle metadata off the status(init) event.
        if kind == "status" and data.get("subtype") == "init":
            sid = data.get("session_id")
            if isinstance(sid, str) and sid != session.cli_session_id:
                session.cli_session_id = sid
                _get_dao().update_instance_cli_session_id(session.id, sid)
            cmds = data.get("slash_commands")
            if isinstance(cmds, list):
                session.claude_slash_commands = [
                    c for c in cmds if isinstance(c, str)
                ]
            init_model = data.get("model") or session.model
            session.context_max = _lookup_context_max(init_model)

        _update_usage_from_data(session, data)

        if kind == "partial":
            # Live-only: accumulate + publish a growing bubble; never persist.
            mid = _message_id_for(event, data)
            piece = getattr(event, "text", None) or ""
            acc = session._partial_accum.get(mid, "") + piece
            session._partial_accum[mid] = acc
            agent_bus.publish_act(session.project, session.scope, {
                "id": getattr(event, "id", mid),
                "kind": "partial",
                "body": acc,
                "meta": {"message_id": mid, "data": data},
                "ts": now_ms(),
            })
            continue

        if kind == "message":
            # Finalize: upsert the one durable row keyed by message_id,
            # accumulating across a turn's text blocks.
            mid = _message_id_for(event, data)
            text = getattr(event, "text", None) or ""
            prior = session._msg_accum.get(mid)
            acc = f"{prior}\n{text}" if prior else text
            session._msg_accum[mid] = acc
            session._partial_accum.pop(mid, None)
            act = agent_transcript.upsert_message_act(
                session, mid, acc, data, now_ms())
            if act is not None:
                agent_bus.publish_act(session.project, session.scope, act)
            continue

        # Persist the act (with its uuid) and fan it out to live subscribers.
        act = agent_transcript.record_event(session, event)
        if act is not None:
            agent_bus.publish_act(session.project, session.scope, act)

        # The terminal `result` closes the turn — reset the accumulators so a
        # long-lived session doesn't accrete per-turn message ids.
        if kind == "result":
            session._partial_accum.clear()
            session._msg_accum.clear()
            # Outer-loop turn boundary: drive task-bound workers (decrement the
            # hard turn budget, inject the next prompt / force-fail at 0).
            # Conversational sessions short-circuit on this single check — no
            # behavioural change, no injected turns.
            if getattr(session, "mode", "conversational") != "conversational":
                from awm.agents import placement
                try:
                    await placement.on_turn_boundary(session)
                except Exception:  # noqa: BLE001
                    pass


# ---------------------------------------------------------------------------
# Waiter
# ---------------------------------------------------------------------------

async def _waiter_loop(session: AgentInstance) -> None:
    proc = session.proc
    if proc is None:
        return
    exit_code = await proc.wait()
    session.exit_code = exit_code
    session.exited_at_ms = now_ms()
    final = "killed" if session.status == "killed" else "exited"
    session.status = final

    _get_dao().close_instance(session.id, ended_at=now_ms(), exit_code=exit_code)

    # Liveness: a task-bound subprocess that died without a terminal outcome
    # must not strand its task in ACTIVE — force-fail it so the orchestrator can
    # re-place. A clean terminal or an intentional stop/respawn no-ops inside.
    if getattr(session, "mode", "conversational") != "conversational":
        from awm.agents import placement
        try:
            await placement.reclaim_if_dead(session)
        except Exception:  # noqa: BLE001
            pass

    if session.input_pump_task is not None:
        session.input_pump_task.cancel()
    if session.scope_poll_task is not None:
        session.scope_poll_task.cancel()

    async with _registry_lock:
        if _by_scope.get(session.scope_key) is session:
            _by_scope.pop(session.scope_key, None)
        _registry_by_id.pop(session.id, None)

    # A scope IS the channel and outlives any single session — there are no
    # rooms to auto-close. The scope's channel + transcript persist.


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

        # Carry the placement forward across respawn so the orchestrator keeps
        # seeing the same agent + task. The placement_token is stable, so the
        # OLD row must release it before the new row reinserts it (the partial
        # unique index forbids two rows holding the same token).
        task_mode = current.mode
        task_ref = current.task_ref
        agent_ref = current.agent_ref
        placement_token = current.placement_token
        task_workdir = current.workdir
        task_allowed_tools = current.allowed_tools
        if task_mode != "conversational" and placement_token is not None:
            _get_dao().clear_placement_token(current.id)

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
        mode=task_mode,
        task_ref=task_ref,
        agent_ref=agent_ref,
        placement_token=placement_token,
        workdir=task_workdir,
        allowed_tools=task_allowed_tools,
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
                  status: str | None = None,
                  limit: int | None = None) -> list[AgentSessionInfo]:
    # DAO returns rows newest-first (ORDER BY id DESC). The optional `limit`
    # caps to the most recent N — applied AFTER the Python-side status filter,
    # so a status filter never undercounts the cap.
    rows = _get_dao().list_instances(project=project, scope=scope)
    out: list[AgentSessionInfo] = []
    for r in rows:
        hydrated = _hydrate_instance_row(r)
        if status and hydrated["render_status"] != status:
            continue
        out.append(_info_for_instance_row(hydrated))
    if limit is not None:
        out = out[: int(limit)]
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

def _dispatch_local_post(scope_key: str, post_author: str,
                         post_body: str) -> None:
    """Forward a scope-channel post to the in-memory session's input queue."""
    session = _by_scope.get(scope_key)
    if session is None:
        return
    enqueue_input(session, post_author, post_body)


def install_room_dispatchers() -> None:
    """No-op in modular mode.

    In the monolith, this wired rooms_svc callbacks. In the modular world,
    room posts arrive via gatewayclient RPC (the scopes service routes them
    here via the agent's registered RPC handler). The hub_adapter wires
    those handlers on startup.
    """
    pass
