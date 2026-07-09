"""Agent instance management — tracked, addressable (modular v1).

An AgentInstance owns one ``claude`` or ``opencode`` subprocess attached to a
single ``scope`` (the globally-unique workspace-unit slug). Its job is the
*agent runtime*: serialize inputs into stdin, parse stdout, and record rendered
text/tool events to the agent's transcript.

Subscribe to agents
-------------------
Raw agent acts (the full structured stdout event stream) belong to the *agent*
and stay local in ``agents.db`` (``agent_transcript``) — you subscribe to an
*agent* for those, keyed on its ``scope`` (the unit slug).

Modular changes from the monolith
----------------------------------
- **Identity** is the ``scope`` (unit slug) ALONE — globally unique, no uuid
  ``agent_id`` and no project namespace.
- **Persistence** goes through ``AgentsDAO`` → ``agents.db``; no shared
  ``state.db``.
- **Raw act writes** go to the ``agent_transcript`` table in ``agents.db``
  via ``agent_transcript.record_*`` (which uses AgentsDAO).
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import signal
import time
from pathlib import Path
from typing import Optional

from awm import config
from awm.agents import dao as _dao_module
from awm.agents.dao import AgentsDAO
from awm.agents._time import now_ms, ms_to_iso
from awm.agents.models import AgentSessionInfo

from awm.agentcore import AgentConfig, open_agent
from awm.agentcore.session import AgentSession as _CoreSession


_SUPPORTED_CLIS = {"claude", "opencode"}
_INPUT_QUEUE_SIZE = 128


def _normalize_cli(value: object) -> str:
    """Map a persisted/legacy ``agent_cli`` onto a current one.

    ``claude-tmux`` was folded into ``claude`` when tmux became the only claude
    backend — a row minted under the old name still reads back as ``claude``."""
    cli = str(value) if value else "claude"
    return "claude" if cli == "claude-tmux" else cli

# Supervision (T4): a task-bound worker gets a hard turn budget — every turn
# boundary decrements it, with NO extension and NO refill. The final stretch
# escalates to a warning so the worker checkpoints + self-fails gracefully.
# These live here (not placement.py) because AgentInstance.__init__ seeds the
# per-session counter; placement.py reads them back for the driver.
TASK_TURN_BUDGET = 100
TASK_WARN_REMAINING = 15

# Module-level DAO instance (initialized after dao.init() is called).
_dao: AgentsDAO | None = None


def _get_dao() -> AgentsDAO:
    global _dao
    if _dao is None:
        _dao = AgentsDAO()
    return _dao


# ---------------------------------------------------------------------------
# AgentInstance
# ---------------------------------------------------------------------------

class AgentInstance:
    """In-memory handle for a running CLI subprocess.

    ``id`` is the ``agent_instances.id`` (per-spawn integer). ``scope`` (the
    globally-unique unit slug) is the natural key — no ``agent_id`` uuid is
    stored.

    The subprocess + stream parsing are owned by an agentcore
    :class:`AgentSession` (``agent_session``); this class keeps the supervisor
    concerns (registry, transcript, attach, resume, slash/compact). The
    ``proc`` property surfaces the agentcore session's underlying process so the
    existing stop/kill/slash/pid paths keep working unchanged.
    """

    def __init__(
        self,
        *,
        id: int,
        scope: str,
        agent_cli: str,
        log_path: Path,
        agent_session: _CoreSession,
    ):
        self.id = id
        self.scope = scope
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
        self.stdin_ready: asyncio.Event = asyncio.Event()
        self.stdin_frames_log = log_path.parent / "agent.log"
        self.permission_mode: str = "default"
        self.model: Optional[str] = None
        self.effort: Optional[str] = None
        self.cli_session_id: Optional[str] = None
        self.claude_slash_commands: list[str] = []
        self.context_used: int = 0
        self.context_max: Optional[int] = None
        # Stall-watchdog clock (T5): a monotonic timestamp stamped on every
        # harness event in ``_reader_loop`` (the single event chokepoint). Seeded
        # at spawn so the clock starts at PLACEMENT — a slow first event never
        # trips the watchdog. The 60s sweep fails a placement silent past
        # ``AWM_PLACEMENT_STALL_S`` (see ``placement.stall_watchdog_loop``).
        self.last_activity: float = time.monotonic()
        # Auto-compact cooldown (T5): the monotonic time of the last ``/compact``
        # injection (0.0 = never). ``placement._maybe_compact`` guards re-fire.
        self.last_compact_at: float = 0.0
        self.respawn_lock: asyncio.Lock = asyncio.Lock()
        # Task-bounded placement: the placement IS this instance row.
        # placement_token names it, agent_ref is the stable placement identity,
        # task_ref binds it to the task. (Overwritten in create_session.)
        self.mode: str = "worker"
        self.task_ref: Optional[str] = None
        self.agent_ref: Optional[str] = None
        self.placement_token: Optional[str] = None
        # Placement workdir (the workspace unit path) + the per-mode tool
        # profile; both carried across respawn. Default-empty for conversational.
        self.workdir: Optional[str] = None
        self.allowed_tools: Optional[list[str]] = None
        # tmux session name for a claude agent (human-attachable); None for
        # opencode (no tmux session).
        self.tmux_session: Optional[str] = None
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


# In-memory registries keyed on scope (the unit slug) and instance id.
_registry_by_id: dict[int, AgentInstance] = {}
_by_scope: dict[str, AgentInstance] = {}
_registry_lock = asyncio.Lock()


class ScopeBusyError(Exception):
    """A second AgentInstance spawn for an already-running scope was attempted."""


class NoSessionError(Exception):
    """No agent instance exists for the requested scope."""


# ---------------------------------------------------------------------------
# Subprocess argv builders
# ---------------------------------------------------------------------------

_VALID_PERMISSION_MODES = (
    "default", "acceptEdits", "auto", "bypassPermissions", "dontAsk", "plan",
)
_VALID_EFFORTS = ("low", "medium", "high", "xhigh", "max")


# ---------------------------------------------------------------------------
# agentcore config builder
# ---------------------------------------------------------------------------

def _tmux_session_name(instance_id: int, scope: str) -> str:
    """Deterministic, tmux-safe session name for a ``claude`` agent.

    ``awm-<instance_id>-<scope>`` with anything outside ``[A-Za-z0-9_-]``
    collapsed to ``-`` (tmux treats ``.`` / ``:`` specially in target names).
    Predictable from ``agent_list`` so a human knows what to ``tmux attach``."""
    safe_scope = re.sub(r"[^A-Za-z0-9_-]", "-", scope)
    return f"awm-{instance_id}-{safe_scope}"


def _build_core_config(
    *, agent_cli: str, permission_mode: str, model: Optional[str],
    effort: Optional[str], resume_session_id: Optional[str],
    workspace_dir: Path,
    allowed_tools: Optional[list[str]] = None,
    disallowed_tools: Optional[list[str]] = None,
    placement_as: Optional[str] = None,
    tmux_session_name: Optional[str] = None,
) -> AgentConfig:
    """Map the agents-service spawn args onto an agentcore :class:`AgentConfig`.

    ``permission_mode == 'bypassPermissions'`` → full-open (``permissions='full'``);
    everything else maps to ``permissions='default'`` (the harness's own
    default). ``effort`` rides ``params`` (claude). ``allowed_tools`` /
    ``disallowed_tools`` are the per-placement tool profile (a task-bound worker
    is full-open on permissions but its tools — fs built-ins + which worker MCP
    tools — are scoped by the allowlist; a conversational session passes neither
    and is unrestricted).

    MCP setup is **harness-owned**: we don't write any config file here. We
    thread the hub's canonical workspace + port (so the harness can synthesize
    the ``awm`` MCP server pointing back at THIS hub) and, for a placement,
    ``placement_as`` (the agent's identity — the unit slug → the synthesized
    server's ``AWM_AS`` → ``X-Awm-As`` → the B-op tools resolve to its own task
    without a model-supplied token). ``placement_as=None`` carries no identity."""
    permissions = "full" if permission_mode == "bypassPermissions" else "default"
    params: dict = {}
    if effort:
        params["effort"] = effort
    return AgentConfig(
        harness=agent_cli,
        mode="live",
        model=model,
        params=params,
        permissions=permissions,
        workdir=str(workspace_dir),
        resume_id=resume_session_id,
        awm_workspace=str(config.canonical_workspace()),
        awm_port=str(config.PORT),
        placement_as=placement_as,
        allowed_tools=allowed_tools,
        disallowed_tools=disallowed_tools,
        tmux_session_name=tmux_session_name,
    )


# ---------------------------------------------------------------------------
# Public API: create / lookup
# ---------------------------------------------------------------------------

async def create_session(*, scope: str,
                         agent_cli: str = "claude",
                         permission_mode: str = "default",
                         model: Optional[str] = None,
                         effort: Optional[str] = None,
                         resume_session_id: Optional[str] = None,
                         fresh: bool = False,
                         mode: str = "worker",
                         task_ref: Optional[str] = None,
                         agent_ref: Optional[str] = None,
                         placement_token: Optional[str] = None,
                         workdir: Optional[str] = None,
                         allowed_tools: Optional[list[str]] = None,
                         disallowed_tools: Optional[list[str]] = None,
                         ) -> AgentInstance:
    """Spawn a CLI subprocess for a placement and register it.

    Every session is a task-bounded **placement**: it runs in a workspace UNIT
    (``workdir``, required — provisioned by the workspace service), NEVER a git
    scope, and never touches the scopes service. ``scope`` (the globally-unique
    unit slug) is the natural key. There is no conversational mode: a human
    attaches via the transcript WS and pushes text via ``agent_post`` (direct
    enqueue). ``mode`` is the placement mode (worker/plan/planner/verify) and
    ``allowed_tools`` / ``disallowed_tools`` carry its tool profile. The DAO
    insert records the placement row and the supervision driver owns the
    lifecycle."""
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
    if not workdir:
        raise ValueError(
            "a placement requires a workdir (the workspace unit path)")

    key = scope
    # The workdir is the workspace UNIT (provisioned by the workspace service),
    # not a git scope under PROJECTS_DIR.
    workspace_dir = Path(workdir)
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

        dao = _get_dao()
        # Resume id recovery.
        if resume_session_id is None and not fresh:
            resume_session_id = dao.get_latest_cli_session_id(scope)

        instance_id = dao.open_task_instance(
            scope=scope,
            log_path=str(log_path),
            cli_session_id=resume_session_id,
            started_at=now_ms(),
            mode=mode,
            task_ref=task_ref,
            agent_ref=agent_ref,
            placement_token=placement_token,
        )

        # Build the agentcore config and drive an AgentSession. The subprocess
        # + stream parsing live in agentcore now; we keep only the supervisor.
        # MCP setup is harness-owned: the placement passes its identity (the
        # unit slug) so the harness synthesizes an awm server with AWM_AS, and
        # its B-op tools resolve to its own task without a token.
        placement_as = scope
        # A claude agent runs in tmux and gets a deterministic session name
        # (human-attachable); opencode has no tmux session.
        tmux_name = (
            _tmux_session_name(instance_id, scope)
            if agent_cli == "claude" else None
        )
        core_config = _build_core_config(
            agent_cli=agent_cli, permission_mode=permission_mode,
            model=model, effort=effort, resume_session_id=resume_session_id,
            workspace_dir=workspace_dir,
            allowed_tools=allowed_tools, disallowed_tools=disallowed_tools,
            placement_as=placement_as,
            tmux_session_name=tmux_name,
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
        session.tmux_session = tmux_name
        # Persist agent_cli (not a column) so lists/hydration report the harness
        # correctly, plus the tmux session name for human attach.
        data_patch: dict = {"agent_cli": agent_cli}
        if tmux_name:
            data_patch["tmux_session"] = tmux_name
        dao.merge_instance_data(instance_id, data_patch)
        _registry_by_id[instance_id] = session
        _by_scope[key] = session

    session.reader_task = asyncio.create_task(_reader_loop(session, event_stream))
    session.waiter_task = asyncio.create_task(_waiter_loop(session))
    session.input_pump_task = asyncio.create_task(_input_pump(session))
    # No scope-channel delivery loop: a human attaches via the transcript WS and
    # pushes text via agent_post (direct enqueue) — every node is a placement.
    try:
        await asyncio.wait_for(session.stdin_ready.wait(), timeout=10.0)
    except asyncio.TimeoutError:
        # Tear down through the seam so a proc-less harness (tmux) is cleaned
        # up too rather than leaking a detached session.
        try:
            await session.agent_session.close()
        except Exception:  # noqa: BLE001
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


def get_session_by_scope(scope: str) -> AgentInstance | None:
    return _by_scope.get(scope)


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
            post_author, post_body, client_id = await session.input_queue.get()
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
        # The turn has begun (this send is the single chokepoint for human AND
        # autonomous/supervisor turns) — publish the transient "working" presence
        # act so the chat shows the agent acting from turn-start, before its first
        # token. The frontend clears it on the turn's first real content / result.
        from awm.agents import agent_transcript
        agent_transcript.publish_working(session)
        try:
            with session.stdin_frames_log.open("a", encoding="utf-8") as fp:
                fp.write(f"STDIN {framed_body!r}\n")
        except OSError:
            pass
        # record_in upserts under the client correlation id (when supplied) so
        # the browser's optimistic chip reconciles in place, stamps the row
        # `delivered`, and publishes it live (the human turn streams back).
        agent_transcript.record_in(
            session, framed_body, injection=False, act_id=client_id)


def enqueue_input(session: AgentInstance, post_author: str,
                  post_body: str, client_id: str | None = None) -> bool:
    """Enqueue a scope-channel post for the agent's stdin pump.

    This is the PASSIVE input channel: the post is queued and the agent consumes
    it between turns (turn-aligned). For a mid-turn forced-interrupt, see
    :func:`notify_agent`. ``client_id`` is an optional browser correlation id
    threaded to ``record_in`` so an optimistic chip reconciles to the recorded
    row in place; internal callers (kickoff/supervisor) omit it."""
    try:
        session.input_queue.put_nowait((post_author, post_body, client_id))
    except asyncio.QueueFull:
        return False
    # Browser-originated post (carries a client_id): publish the human turn to
    # the live bus NOW, so a connected chat sees it immediately instead of only
    # when the agent next takes a turn (the slow-read bug). ``record_in`` later
    # upserts the same id; the two fold to one row. Internal callers (kickoff /
    # supervisor) omit client_id and so never publish a spurious human turn.
    if client_id:
        from awm.agents import agent_transcript
        framed_body = f"[from:{post_author}]\n{post_body}"
        agent_transcript.publish_inbound(session, framed_body, client_id)
        # A new human message clears the agent's detach-readiness: new information
        # means it must re-confirm before it can detach. No-op unless the task is
        # attached with the readiness bit set. (Browser posts only — kickoff /
        # supervisor turns carry no client_id and never touch the bit.)
        from awm.agents import placement
        placement.note_user_post(session)
    return True


async def notify_agent(session: AgentInstance, author: str, body: str) -> bool:
    """FORCED-INTERRUPT notification: preempt the agent's current turn.

    The third input channel (with passive ``enqueue_input`` and the human's
    direct terminal keystrokes): an operator or another agent forces a message
    in mid-turn via the harness :meth:`AgentSession.interrupt` seam — the tmux
    harness sends ESC to cancel the in-flight turn, then pastes ``body``; a
    headless harness falls back to a plain ``send``. The agent's OWN posts are
    filtered (never self-notify), mirroring the passive path. Recorded to the
    transcript as an injection. Returns False when filtered or the harness
    rejects the interrupt."""
    if _is_own_author(session, author):
        return False
    framed = f"[notify:{author}]\n{body}"
    try:
        await session.agent_session.interrupt(framed)
    except Exception:  # noqa: BLE001
        return False
    try:
        with session.stdin_frames_log.open("a", encoding="utf-8") as fp:
            fp.write(f"STDIN(notify) {framed!r}\n")
    except OSError:
        pass
    from awm.agents import agent_transcript
    agent_transcript.record_in(session, framed, injection=True)
    return True


# ---------------------------------------------------------------------------
# Own-author filter (don't deliver an agent's own output back to itself)
# ---------------------------------------------------------------------------

def _is_own_author(session: AgentInstance, author: str) -> bool:
    """True if `author` is this agent's own scope ref (don't self-deliver)."""
    if not author:
        return False
    a = author
    if a.startswith(("agent:", "scope:")):
        a = a.split(":", 1)[1]
    return a == session.scope


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
        # Stall-watchdog heartbeat (T5): EVERY harness event for this placed
        # session (turn/tool/status/transcript) flows through here — this is the
        # single chokepoint, so one stamp measures a long silent gap. A stalled
        # placement is one that emits nothing for AWM_PLACEMENT_STALL_S.
        session.last_activity = time.monotonic()
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
            agent_bus.publish_act(session.scope, {
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
                agent_bus.publish_act(session.scope, act)
            continue

        # Persist the act (with its uuid) and fan it out to live subscribers.
        act = agent_transcript.record_event(session, event)
        if act is not None:
            agent_bus.publish_act(session.scope, act)

        # The terminal `result` closes the turn — reset the accumulators so a
        # long-lived session doesn't accrete per-turn message ids.
        if kind == "result":
            session._partial_accum.clear()
            session._msg_accum.clear()
            # Outer-loop turn boundary: drive the session's supervisor —
            # a game bot gets the gamebot driver (budget countdown →
            # force-park), everything else the placement driver (decrement
            # the hard turn budget, inject the next prompt / force-fail at 0).
            try:
                if getattr(session, "mode", None) == "gamebot":
                    from awm.agents import gamebot
                    await gamebot.on_turn_boundary(session)
                else:
                    from awm.agents import placement
                    await placement.on_turn_boundary(session)
            except Exception:  # noqa: BLE001
                pass


# ---------------------------------------------------------------------------
# Waiter
# ---------------------------------------------------------------------------

async def _waiter_loop(session: AgentInstance) -> None:
    # Harness-agnostic liveness: subprocess backends await their child; the
    # tmux backend polls its detached session. Both surface through wait().
    try:
        exit_code = await session.agent_session.wait()
    except NotImplementedError:
        return
    session.exit_code = exit_code if exit_code is not None else 0
    session.exited_at_ms = now_ms()
    final = "killed" if session.status == "killed" else "exited"
    session.status = final

    _get_dao().close_instance(session.id, ended_at=now_ms(),
                              exit_code=session.exit_code)

    # Liveness: a placement subprocess that died without a terminal outcome must
    # not strand its task in ACTIVE — force-fail it so the orchestrator can
    # re-place. A clean terminal or an intentional stop/respawn no-ops inside.
    from awm.agents import placement
    try:
        await placement.reclaim_if_dead(session)
    except Exception:  # noqa: BLE001
        pass

    if session.input_pump_task is not None:
        session.input_pump_task.cancel()

    async with _registry_lock:
        if _by_scope.get(session.scope) is session:
            _by_scope.pop(session.scope, None)
        _registry_by_id.pop(session.id, None)

    # The scope outlives any single session — there is nothing to auto-close.
    # The scope's transcript persists.


# ---------------------------------------------------------------------------
# Stop / kill
# ---------------------------------------------------------------------------

def _info_for_instance_row(row: dict) -> AgentSessionInfo:
    return AgentSessionInfo(
        id=row["id"],
        scope=row["scope"],
        pid=row.get("pid") or 0,
        status=row.get("render_status") or "exited",
        agent_cli=_normalize_cli(row.get("agent_cli")),
        started_at=ms_to_iso(row["started_at"]) or "",
        exited_at=ms_to_iso(row.get("ended_at")),
        exit_code=row.get("exit_code"),
        attached=False,
        tmux_session=row.get("tmux_session"),
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
    pid = (instance_handle.proc.pid
           if instance_handle and instance_handle.proc else 0)
    try:
        data = json.loads(row.get("data") or "{}")
    except (TypeError, ValueError):
        data = {}
    out = dict(row)
    out["pid"] = pid
    out["exit_code"] = data.get("exit_code")
    out["render_status"] = _render_status(out)
    # agent_cli: not in agents.db; recover from data, default "claude", and
    # fold the legacy "claude-tmux" name onto "claude".
    out["agent_cli"] = _normalize_cli(out.get("agent_cli") or data.get("agent_cli"))
    out["tmux_session"] = data.get("tmux_session")
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
    # Tear down through the harness seam (subprocess terminate / tmux
    # kill-session) so the waiter loop observes the exit and deregisters.
    try:
        await session.agent_session.close()
    except Exception:  # noqa: BLE001
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
        await session.agent_session.close()
    except Exception:  # noqa: BLE001
        pass
    return _info_for_instance_row(_row_for_instance(session_id) or row)


# ---------------------------------------------------------------------------
# Respawn + slash-input primitives
# ---------------------------------------------------------------------------

async def respawn_session(
    scope: str, *,
    force: bool = False,
    permission_mode: Optional[str] = None,
    model: Optional[str] = None,
    effort: Optional[str] = None,
    agent_cli: Optional[str] = None,
    clear_history: bool = False,
) -> AgentInstance:
    current = _by_scope.get(scope)
    if current is None:
        raise NoSessionError(f"no active session for {scope}")

    async with current.respawn_lock:
        new_mode = permission_mode if permission_mode is not None else current.permission_mode
        new_model = model if model is not None else current.model
        new_effort = effort if effort is not None else current.effort
        new_cli = agent_cli if agent_cli is not None else current.agent_cli
        resume_sid = None if clear_history else current.cli_session_id
        scope = current.scope

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
        if placement_token is not None:
            _get_dao().clear_placement_token(current.id)

        if force:
            await kill_session(current.id)
        else:
            await stop_session(current.id)
        for _ in range(60):
            if _by_scope.get(scope) is None:
                break
            await asyncio.sleep(0.05)
        else:
            await kill_session(current.id)
            for _ in range(40):
                if _by_scope.get(scope) is None:
                    break
                await asyncio.sleep(0.05)

    return await create_session(
        scope=scope,
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
    """Forward a raw line into the agent's interactive TUI.

    Pasted verbatim (NO ``[from:author]`` framing) so a leading-slash line runs
    as claude's native slash command (``/compact``, ``/clear``, plugin commands,
    …). Unframed and immediate, the native-passthrough successor to the old
    headless stdin write. Recorded to the transcript as an injection."""
    session = _by_scope.get(scope_key)
    if session is None:
        raise NoSessionError(f"no active session for {scope_key}")
    try:
        await session.agent_session.send(body)
    except Exception as exc:  # noqa: BLE001
        raise NoSessionError(
            f"session for {scope_key} cannot accept input: {exc}"
        ) from exc
    try:
        with session.stdin_frames_log.open("a", encoding="utf-8") as fp:
            fp.write(f"STDIN(slash) {body!r}\n")
    except OSError:
        pass
    from awm.agents import agent_transcript
    agent_transcript.record_in(session, body, injection=True)


# ---------------------------------------------------------------------------
# Query helpers
# ---------------------------------------------------------------------------

def list_sessions(scope: str | None = None,
                  status: str | None = None,
                  limit: int | None = None) -> list[AgentSessionInfo]:
    # DAO returns rows newest-first (ORDER BY id DESC). The optional `limit`
    # caps to the most recent N — applied AFTER the Python-side status filter,
    # so a status filter never undercounts the cap.
    rows = _get_dao().list_instances(scope=scope)
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


# ---------------------------------------------------------------------------
# Reconciliation on startup
# ---------------------------------------------------------------------------

def reconcile_on_startup() -> None:
    """Close any agent_instances rows left open by a prior run.

    A placement whose subprocess is gone after a restart is NOT auto-resumed by
    this service — the orchestrator owns re-dispatch (its boot reconcile
    re-places resting nodes; an out-state node's liveness is reported back via
    ``orch.fail``). So boot is a pure cleanup: close stale rows, nothing more."""
    _get_dao().close_all_open_instances(now_ms())


