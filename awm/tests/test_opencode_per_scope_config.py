"""Tests for the per-scope opencode config that auto-loads `.awm/context.md`.

At scope-create time, `_write_scope_opencode_config` writes
`<worktree>/.awm/mcp-opencode.json` that (a) inherits the workspace MCP
catalog when present and (b) adds `instructions: [".awm/context.md"]` so
opencode loads scope orientation natively on startup. The session-launch
path in `agent_instances.create_session` prefers the per-scope file over
the workspace fallback.
"""

from __future__ import annotations

import json
from pathlib import Path

from awm.services.scopes import _write_scope_opencode_config


class TestWriteScopeOpencodeConfig:
    def test_creates_file_with_instructions(self, tmp_path, awm_workspace):
        wt = tmp_path / "wt"
        awm = wt / ".awm"
        awm.mkdir(parents=True)

        _write_scope_opencode_config(awm)

        cfg_path = awm / "mcp-opencode.json"
        assert cfg_path.is_file()
        cfg = json.loads(cfg_path.read_text())
        assert cfg["instructions"] == [".awm/context.md"]
        # MCP catalog defaults to empty when workspace config absent.
        assert cfg["mcp"] == {}

    def test_inherits_workspace_mcp_servers(self, tmp_path, awm_workspace):
        workspace_cfg = awm_workspace["awm_dir"] / "mcp-opencode.json"
        workspace_cfg.write_text(json.dumps({
            "$schema": "https://opencode.ai/config.json",
            "mcp": {
                "awm": {"type": "local", "enabled": True,
                        "command": ["awm", "serve-exposed"],
                        "environment": {}},
            },
        }))
        wt = tmp_path / "wt"
        awm = wt / ".awm"
        awm.mkdir(parents=True)

        _write_scope_opencode_config(awm)

        cfg = json.loads((awm / "mcp-opencode.json").read_text())
        assert "awm" in cfg["mcp"]
        assert cfg["mcp"]["awm"]["command"] == ["awm", "serve-exposed"]
        assert cfg["instructions"] == [".awm/context.md"]

    def test_idempotent_does_not_duplicate_instructions(self, tmp_path, awm_workspace):
        wt = tmp_path / "wt"
        awm = wt / ".awm"
        awm.mkdir(parents=True)

        _write_scope_opencode_config(awm)
        _write_scope_opencode_config(awm)

        cfg = json.loads((awm / "mcp-opencode.json").read_text())
        assert cfg["instructions"] == [".awm/context.md"]

    def test_survives_corrupt_workspace_config(self, tmp_path, awm_workspace):
        workspace_cfg = awm_workspace["awm_dir"] / "mcp-opencode.json"
        workspace_cfg.write_text("not json{")
        wt = tmp_path / "wt"
        awm = wt / ".awm"
        awm.mkdir(parents=True)

        _write_scope_opencode_config(awm)

        cfg = json.loads((awm / "mcp-opencode.json").read_text())
        assert cfg["instructions"] == [".awm/context.md"]
        assert cfg["mcp"] == {}
