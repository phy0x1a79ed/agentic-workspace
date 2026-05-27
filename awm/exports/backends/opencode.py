"""opencode MCP-config exporter.

Translates Claude Code's ``.mcp.json`` shape into opencode's config shape and
writes it to ``<workspace>/.awm/mcp-opencode.json``. The consumer side wires
opencode to that file by setting ``OPENCODE_CONFIG`` (merges with user config).
"""

from __future__ import annotations

import json
from pathlib import Path

from awm.config import WORKSPACE_ROOT
from awm.exports.mcp import register


class OpencodeExporter:
    name = "opencode"

    def export(self, canonical: dict) -> tuple[Path, str]:
        servers = canonical.get("mcpServers", {})
        out: dict = {"$schema": "https://opencode.ai/config.json", "mcp": {}}
        for key, entry in servers.items():
            cmd = [entry["command"], *entry.get("args", [])]
            out["mcp"][key] = {
                "type": "local",
                "enabled": True,
                "command": cmd,
                "environment": entry.get("env", {}),
            }
        path = WORKSPACE_ROOT / ".awm" / "mcp-opencode.json"
        return path, json.dumps(out, indent=2) + "\n"


register(OpencodeExporter())
