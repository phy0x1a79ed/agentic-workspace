"""Harness-owned MCP config synthesis.

Each harness owns setting up its OWN agent — including the MCP config that wires
a spawned agent back to the awm hub. agentcore builds the ``awm`` MCP server
entry from first principles (the ``awm-mcp`` stdio proxy + the hub's port +
canonical workspace), so it depends on NO gateway-exported file. For a
**placement** it also injects ``AWM_AS`` — the placed agent's identity — so its
B-op tools (``edit_deliverable`` / ``indicate_done`` / planner graph tools)
resolve to its own task without the model ever supplying a token (the proxy
stamps ``X-Awm-As`` from ``AWM_AS`` on every ``/invoke``).

The two write helpers emit the harness-native shape:
- claude:   ``<workdir>/.awm/spawn-mcp.json`` (``--strict-mcp-config``)
- opencode: ``<workdir>/.awm/mcp-opencode.json`` (pointed at via ``OPENCODE_CONFIG``)

Leaf module: no awm.config / gateway imports — the hub workspace + port are
threaded in via :class:`AgentConfig`, not read from awm.config.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Optional

from ._path import resolve_bin
from .types import AgentConfig


def _resolve_awm_mcp() -> str:
    """Resolve the ``awm-mcp`` stdio proxy command for a spawned agent.

    Prefer the binary that sits **next to the running interpreter**
    (``<sys.executable dir>/awm-mcp``) — i.e. the in-env console script. That
    path execs the proxy DIRECTLY with unbuffered stdio, which the MCP JSON-RPC
    stdio handshake requires. The PATH-searched ``resolve_bin`` would otherwise
    pick a ``~/.local/bin/awm-mcp`` shim that wraps ``mamba run`` WITHOUT
    ``--no-capture-output``; its buffered stdout never flushes the handshake, so
    the spawned worker's awm MCP server silently never initializes. Fall back to
    ``resolve_bin`` only when the in-env sibling is absent."""
    sibling = Path(sys.executable).resolve().parent / "awm-mcp"
    if sibling.exists():
        return str(sibling)
    return resolve_bin("awm-mcp")


def _awm_server_env(config: AgentConfig) -> dict[str, str]:
    """The env for the spawned ``awm-mcp`` proxy: which hub + which identity.

    ``AWM_PORT`` is load-bearing — it selects which gateway the proxy talks to
    (``config.BASE_URL`` = ``http://127.0.0.1:$AWM_PORT``). ``AWM_WORKSPACE`` is
    the canonical workspace (for the proxy's own env-file load). ``AWM_AS`` is
    present only for placements (the caller-identity B-op resolution)."""
    env: dict[str, str] = {}
    if config.awm_workspace:
        env["AWM_WORKSPACE"] = str(config.awm_workspace)
    if config.awm_port:
        env["AWM_PORT"] = str(config.awm_port)
    if config.placement_as:
        env["AWM_AS"] = str(config.placement_as)
    return env


def _wants_awm_server(config: AgentConfig) -> bool:
    """True when the caller asked for the awm MCP server (a hub port is set)."""
    return bool(config.awm_port)


def _awm_dir(config: AgentConfig) -> Optional[Path]:
    if not config.workdir:
        return None
    d = Path(config.workdir) / ".awm"
    try:
        d.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    return d


def write_claude_mcp_config(config: AgentConfig) -> Optional[str]:
    """Synthesize the claude ``spawn-mcp.json`` for this agent; return its path.

    None when there's no awm server to attach or no workdir to write into — the
    backend then falls back to ``config.mcp_config`` (an explicit override) or
    no MCP config at all."""
    if not _wants_awm_server(config):
        return None
    awm_dir = _awm_dir(config)
    if awm_dir is None:
        return None
    try:
        command = _resolve_awm_mcp()
    except RuntimeError:
        return None
    cfg = {"mcpServers": {"awm": {
        "command": command, "args": [], "env": _awm_server_env(config),
    }}}
    dest = awm_dir / "spawn-mcp.json"
    try:
        dest.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    except OSError:
        return None
    return str(dest)


def write_opencode_mcp_config(config: AgentConfig) -> Optional[str]:
    """Synthesize the opencode ``mcp-opencode.json`` for this agent; return path.

    opencode reads MCP config from the ``OPENCODE_CONFIG`` env (not a CLI flag),
    so the backend sets that env to this path. Same awm server, opencode's
    ``mcp`` shape. None under the same conditions as the claude variant."""
    if not _wants_awm_server(config):
        return None
    awm_dir = _awm_dir(config)
    if awm_dir is None:
        return None
    try:
        command = _resolve_awm_mcp()
    except RuntimeError:
        return None
    cfg = {
        "$schema": "https://opencode.ai/config.json",
        "mcp": {"awm": {
            "type": "local",
            "enabled": True,
            "command": [command],
            "environment": _awm_server_env(config),
        }},
    }
    dest = awm_dir / "mcp-opencode.json"
    try:
        dest.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    except OSError:
        return None
    return str(dest)
