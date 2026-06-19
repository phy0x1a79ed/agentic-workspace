"""Normalized agent-harness types — the keystone contract.

``agentcore`` owns exactly one thing: "talk to a harness subprocess and yield
normalized events." Everything a caller sees is one of:

- :class:`AgentConfig` — the dynamic config that selects a backend (claude or
  opencode), a mode (live or oneshot), and threads model / params / perms /
  workdir / resume / mcp_config / system_prompt into it.
- :class:`AgentSession` — the open handle: ``await send(text)``,
  ``events() -> async-iter[AgentEvent]``, ``await close()``.
- :class:`AgentEvent` — the single normalized event shape both backends map
  onto. This is the contract downstream consumers (agents service, stt
  cleanup, frontend) dedupe and render against.

This module is a LEAF: it imports nothing from ``awm.config`` or any
gateway-side module. Keep it that way.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal, Optional


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

Harness = Literal["claude", "claude-tmux", "opencode"]
Mode = Literal["live", "oneshot"]


@dataclass
class AgentConfig:
    """Dynamic config for one agent session.

    ``harness`` + ``mode`` select the backend and shape; the rest are threaded
    into the subprocess. ``permissions='full'`` (the default) maps to each
    harness's full-open flag (claude ``--permission-mode=bypassPermissions``,
    opencode ``--dangerously-skip-permissions``); set ``permissions='default'``
    to drop the override.

    Fields:
        harness:        'claude' | 'opencode'
        mode:           'live' | 'oneshot'
        model:          model id passed to the harness (``--model``), or None.
        params:         backend-specific extras. Recognized keys:
                          claude:   {'effort': 'low'|'medium'|'high'|'xhigh'|'max'}
                          opencode: {'provider_id': str, 'model_id': str}
                        Unknown keys are ignored by the backend.
        permissions:    'full' (default) | 'default'. 'full' = full-open flag.
        workdir:        cwd for the subprocess (the worktree). None = caller cwd.
        resume_id:      harness session id to resume (claude --resume); opencode
                        uses it as the persistent session id to reattach to.
        mcp_config:     path to an MCP config file to pass to the harness
                        (claude --mcp-config). None = no MCP config.
        system_prompt:  appended-system-prompt text (claude
                        --append-system-prompt). None = none.
        allowed_tools:  explicit tool allowlist (claude --allowedTools). Names
                        may be built-ins (``Read``, ``Write``, ``Edit``,
                        ``Bash``, …) OR MCP tool names
                        (``mcp__awm__edit_deliverable``), so one lever scopes
                        both filesystem access AND which worker tools a placed
                        agent may call. None = no restriction (harness default).
        disallowed_tools: explicit tool denylist (claude --disallowedTools),
                        same name space as ``allowed_tools``. Applied on top of
                        the allowlist. None = no denylist.
        tmux_session_name: for the ``claude-tmux`` harness, the tmux session
                        name to create/attach (so a human can ``tmux attach``).
                        None = the backend mints ``awm-<session-id[:8]>``.
                        Ignored by other harnesses.
    """

    harness: Harness
    mode: Mode = "live"
    model: Optional[str] = None
    params: dict[str, Any] = field(default_factory=dict)
    permissions: Literal["full", "default"] = "full"
    workdir: Optional[str] = None
    resume_id: Optional[str] = None
    mcp_config: Optional[str] = None
    system_prompt: Optional[str] = None
    allowed_tools: Optional[list[str]] = None
    disallowed_tools: Optional[list[str]] = None
    tmux_session_name: Optional[str] = None


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------

EventKind = Literal[
    "message",       # assistant text (a complete text block)
    "partial",       # streaming token delta (high volume; coalesce by message)
    "tool_use",      # the agent invoked a tool
    "tool_result",   # a tool returned
    "status",        # lifecycle / session metadata (init, model, session id)
    "result",        # terminal turn/run result (final, with usage etc.)
    "error",         # an error surfaced by the harness
]


def _new_id() -> str:
    return uuid.uuid4().hex


def _now_ms() -> int:
    return int(time.time() * 1000)


@dataclass
class AgentEvent:
    """One normalized event off the harness stream.

    ``id`` is a stable per-event uuid (the dedupe key end-to-end — a ws
    backfill + live overlap is de-duplicated on it). ``kind`` is the normalized
    classification; ``text`` carries human-renderable text (message / partial /
    tool blurbs); ``data`` carries the structured payload (tool name + input,
    tool result body, usage, raw harness event); ``ts`` is unix-ms.
    """

    kind: EventKind
    text: Optional[str] = None
    data: Optional[dict[str, Any]] = None
    id: str = field(default_factory=_new_id)
    ts: int = field(default_factory=_now_ms)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "text": self.text,
            "data": self.data,
            "ts": self.ts,
        }
