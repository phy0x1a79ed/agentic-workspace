"""Tests for the per-scope opencode config that auto-loads orientation docs.

At scope-create time, `_write_scope_opencode_config` writes
`<worktree>/.awm/mcp-opencode.json` that (a) inherits the workspace MCP
catalog when present and (b) adds an `instructions` array so opencode
loads orientation docs natively on startup. The array always contains
`.awm/context.md` (scope tier) and prepends the absolute path of
`<WORKSPACE_ROOT>/WORKSPACE.md` (workspace tier) when that file exists.
The repo tier (AGENTS.md) is omitted — opencode walks AGENTS.md natively
from cwd, so listing it would double-inject. The session-launch path in
`agent_instances.create_session` prefers the per-scope file over the
workspace fallback.
"""

from __future__ import annotations


import pytest
pytestmark = [pytest.mark.mcp, pytest.mark.smoke]

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

    def test_includes_workspace_md_when_present(self, tmp_path, awm_workspace):
        # When the workspace has a WORKSPACE.md file, its absolute path goes
        # into the instructions array (workspace tier) before .awm/context.md.
        workspace_md = awm_workspace["workspace"] / "WORKSPACE.md"
        workspace_md.write_text("# workspace orientation\n")
        wt = tmp_path / "wt"
        awm = wt / ".awm"
        awm.mkdir(parents=True)

        _write_scope_opencode_config(awm)

        cfg = json.loads((awm / "mcp-opencode.json").read_text())
        assert cfg["instructions"] == [str(workspace_md), ".awm/context.md"]
