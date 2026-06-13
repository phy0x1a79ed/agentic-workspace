"""claude-spawn MCP-config exporter.

Writes ``<workspace>/.awm/spawn-mcp.json`` — the strict-mcp-config file
passed to ``claude`` subprocesses spawned by AWM (see
``awm/services/agent_instances.py``). Because spawned Claudes ignore the
normal ``.mcp.json`` walk-up under ``--strict-mcp-config``, this file is
the *only* surface where they discover MCP servers.

The exporter is a pass-through of the canonical ``.mcp.json`` plus one
override: the ``awm`` entry's env is rewritten with the live
``AWM_WORKSPACE`` + ``AWM_PORT`` of the running listener, so the proxy
talks back to *this* peer regardless of what the static file says.
That preserves the dev/prod isolation the old hand-rolled writer
provided.
"""

from __future__ import annotations

import json
import os
import shutil
from copy import deepcopy
from pathlib import Path

from awm.config import AWM_DIR
from awm.gateway.exports.mcp import register


class ClaudeSpawnExporter:
    name = "claude-spawn"

    def export(self, canonical: dict) -> tuple[Path, str]:
        out = deepcopy(canonical)
        servers = out.setdefault("mcpServers", {})

        awm_mcp = shutil.which("awm-mcp")
        if awm_mcp is not None:
            workspace = os.environ.get("AWM_WORKSPACE")
            port = os.environ.get("AWM_PORT")
            existing = servers.get("awm", {}) if isinstance(servers.get("awm"), dict) else {}
            existing_env = existing.get("env", {}) if isinstance(existing.get("env"), dict) else {}
            env = {**existing_env}
            if workspace:
                env["AWM_WORKSPACE"] = workspace
            if port:
                env["AWM_PORT"] = port
            servers["awm"] = {
                "command": awm_mcp,
                "args": [],
                "env": env,
            }

        path = AWM_DIR / "spawn-mcp.json"
        return path, json.dumps(out, indent=2) + "\n"


register(ClaudeSpawnExporter())
