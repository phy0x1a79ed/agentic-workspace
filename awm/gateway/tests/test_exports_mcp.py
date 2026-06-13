"""Tests for the MCP-config export framework + claude_spawn/opencode backends.

The framework reads canonical workspace ``.mcp.json`` and fans it out to
backend-specific config files. ``claude_spawn`` rewrites the ``awm`` entry
from live env vars so dev/prod isolation survives; ``opencode`` translates
the schema. These tests cover the framework's plumbing (register dedup,
report shape, exporter isolation) and each backend's invariants.
"""

from __future__ import annotations


import pytest
pytestmark = [pytest.mark.mcp, pytest.mark.slow, pytest.mark.subprocess]

import json
import os
from contextlib import contextmanager
from pathlib import Path

import pytest

import awm.gateway.exports.backends.claude_spawn as claude_spawn_mod
import awm.gateway.exports.backends.opencode as opencode_mod
from awm.gateway.exports import mcp as mcp_mod
from awm.gateway.exports.backends.claude_spawn import ClaudeSpawnExporter
from awm.gateway.exports.backends.opencode import OpencodeExporter
from awm.gateway.exports.mcp import EXPORTERS, register, sync_mcp_configs


@contextmanager
def _isolated_registry():
    """Snapshot/restore EXPORTERS so tests can register dummy exporters without
    leaking into the rest of the suite (the backends register themselves on
    import; we want them removed during framework-isolation tests)."""
    saved = list(EXPORTERS)
    EXPORTERS.clear()
    try:
        yield
    finally:
        EXPORTERS.clear()
        EXPORTERS.extend(saved)


class _DummyExporter:
    def __init__(self, name, out_path, content="BODY", raise_exc=None):
        self.name = name
        self.out_path = out_path
        self.content = content
        self.raise_exc = raise_exc
        self.calls = []

    def export(self, canonical):
        self.calls.append(canonical)
        if self.raise_exc is not None:
            raise self.raise_exc
        return self.out_path, self.content


class TestRegister:
    def test_dedupes_by_name(self):
        with _isolated_registry():
            a = _DummyExporter("dup", Path("/tmp/a"))
            b = _DummyExporter("dup", Path("/tmp/b"))
            register(a)
            register(b)
            assert len(EXPORTERS) == 1
            assert EXPORTERS[0] is b

    def test_returns_exporter(self):
        with _isolated_registry():
            a = _DummyExporter("x", Path("/tmp/x"))
            assert register(a) is a


class TestSyncFramework:
    def test_no_mcp_json_returns_error(self, tmp_path):
        with _isolated_registry():
            report = sync_mcp_configs(tmp_path)
        assert report == [{"ok": False, "error": f"no .mcp.json at {tmp_path / '.mcp.json'}"}]

    def test_invalid_json_returns_error(self, tmp_path):
        (tmp_path / ".mcp.json").write_text("{ not json")
        with _isolated_registry():
            report = sync_mcp_configs(tmp_path)
        assert len(report) == 1
        assert report[0]["ok"] is False
        assert "invalid" in report[0]["error"].lower()

    def test_writes_each_exporter_and_reports_ok(self, tmp_path):
        (tmp_path / ".mcp.json").write_text(json.dumps(
            {"mcpServers": {"a": {"command": "x"}}}
        ))
        out = tmp_path / "out.json"
        with _isolated_registry():
            dummy = _DummyExporter("dummy", out, content="BODY")
            register(dummy)
            report = sync_mcp_configs(tmp_path)

        assert len(report) == 1
        entry = report[0]
        assert entry["name"] == "dummy"
        assert entry["ok"] is True
        assert entry["path"] == str(out)
        assert entry["bytes"] == len(b"BODY")
        assert out.read_bytes() == b"BODY"
        # The canonical dict the exporter saw is the parsed .mcp.json.
        assert dummy.calls == [{"mcpServers": {"a": {"command": "x"}}}]

    def test_failing_exporter_does_not_block_others(self, tmp_path):
        (tmp_path / ".mcp.json").write_text(json.dumps({"mcpServers": {}}))
        bad_path = tmp_path / "bad.json"  # never written
        good_path = tmp_path / "good.json"
        with _isolated_registry():
            register(_DummyExporter(
                "bad", bad_path, raise_exc=RuntimeError("boom"),
            ))
            register(_DummyExporter("good", good_path))
            report = sync_mcp_configs(tmp_path)

        by_name = {entry["name"]: entry for entry in report}
        assert by_name["bad"]["ok"] is False
        assert "boom" in by_name["bad"]["error"]
        assert by_name["good"]["ok"] is True
        assert good_path.exists()
        assert not bad_path.exists()

    def test_atomic_write_removes_tmp_file(self, tmp_path):
        (tmp_path / ".mcp.json").write_text(json.dumps({"mcpServers": {}}))
        out = tmp_path / "out.json"
        with _isolated_registry():
            register(_DummyExporter("d", out, content="HELLO"))
            sync_mcp_configs(tmp_path)
        assert out.exists()
        assert not out.with_suffix(out.suffix + ".tmp").exists()


class TestClaudeSpawnExporter:
    """Exercise ClaudeSpawnExporter against the AWM_DIR binding it captured
    at import time. The test patches that binding (not the config module's)."""

    def test_path_is_awm_dir(self, tmp_path, monkeypatch):
        monkeypatch.setattr(claude_spawn_mod, "AWM_DIR", tmp_path)
        path, _ = ClaudeSpawnExporter().export({"mcpServers": {}})
        assert path == tmp_path / "spawn-mcp.json"

    def test_mirrors_canonical_servers(self, tmp_path, monkeypatch):
        monkeypatch.setattr(claude_spawn_mod, "AWM_DIR", tmp_path)
        monkeypatch.setattr(claude_spawn_mod.shutil, "which",
                            lambda name: None)
        canonical = {
            "mcpServers": {
                "chrome-devtools": {"command": "npx", "args": ["x"]},
                "ollama": {"command": "npx", "args": ["y"], "env": {"K": "V"}},
            }
        }
        _, content = ClaudeSpawnExporter().export(canonical)
        out = json.loads(content)
        # All canonical servers carried over verbatim.
        assert out["mcpServers"]["chrome-devtools"] == {"command": "npx", "args": ["x"]}
        assert out["mcpServers"]["ollama"] == {"command": "npx", "args": ["y"], "env": {"K": "V"}}
        # No awm entry injected when the binary isn't on PATH.
        assert "awm" not in out["mcpServers"]

    def test_injects_awm_proxy_when_binary_present(self, tmp_path, monkeypatch):
        monkeypatch.setattr(claude_spawn_mod, "AWM_DIR", tmp_path)
        monkeypatch.setattr(claude_spawn_mod.shutil, "which",
                            lambda name: "/fake/bin/awm-mcp" if name == "awm-mcp" else None)
        monkeypatch.setenv("AWM_WORKSPACE", "/the/ws")
        monkeypatch.setenv("AWM_PORT", "12100")

        _, content = ClaudeSpawnExporter().export({"mcpServers": {}})
        out = json.loads(content)
        awm_entry = out["mcpServers"]["awm"]
        assert awm_entry["command"] == "/fake/bin/awm-mcp"
        assert awm_entry["args"] == []
        assert awm_entry["env"] == {
            "AWM_WORKSPACE": "/the/ws",
            "AWM_PORT": "12100",
        }

    def test_overrides_stale_awm_entry(self, tmp_path, monkeypatch):
        """Canonical .mcp.json may carry a stale awm entry. Live env vars
        win — and pre-existing env keys are preserved when the live env
        doesn't override them."""
        monkeypatch.setattr(claude_spawn_mod, "AWM_DIR", tmp_path)
        monkeypatch.setattr(claude_spawn_mod.shutil, "which",
                            lambda name: "/fake/bin/awm-mcp")
        monkeypatch.setenv("AWM_PORT", "7820")
        monkeypatch.delenv("AWM_WORKSPACE", raising=False)
        canonical = {
            "mcpServers": {
                "awm": {
                    "command": "/old/awm-mcp",
                    "args": ["--legacy"],
                    "env": {
                        "AWM_PORT": "9999",
                        "EXTRA": "kept",
                    },
                }
            }
        }
        _, content = ClaudeSpawnExporter().export(canonical)
        out = json.loads(content)
        awm_entry = out["mcpServers"]["awm"]
        # Command replaced with the live binary path.
        assert awm_entry["command"] == "/fake/bin/awm-mcp"
        assert awm_entry["args"] == []
        # Live env wins; unrelated existing keys preserved; AWM_WORKSPACE
        # never set so not added.
        assert awm_entry["env"] == {
            "AWM_PORT": "7820",
            "EXTRA": "kept",
        }

    def test_returns_valid_json(self, tmp_path, monkeypatch):
        monkeypatch.setattr(claude_spawn_mod, "AWM_DIR", tmp_path)
        monkeypatch.setattr(claude_spawn_mod.shutil, "which",
                            lambda _: None)
        _, content = ClaudeSpawnExporter().export({"mcpServers": {}})
        # Must parse and end with newline so atomic writers don't truncate.
        assert content.endswith("\n")
        assert json.loads(content) == {"mcpServers": {}}


class TestOpencodeExporter:
    def test_path_and_schema(self, tmp_path, monkeypatch):
        monkeypatch.setattr(opencode_mod, "WORKSPACE_ROOT", tmp_path)
        canonical = {
            "mcpServers": {
                "chrome-devtools": {
                    "command": "npx",
                    "args": ["-y", "chrome-devtools-mcp"],
                    "env": {"PATH": "/usr/bin"},
                }
            }
        }
        path, content = OpencodeExporter().export(canonical)
        assert path == tmp_path / ".awm" / "mcp-opencode.json"
        out = json.loads(content)
        assert out["$schema"] == "https://opencode.ai/config.json"
        entry = out["mcp"]["chrome-devtools"]
        assert entry == {
            "type": "local",
            "enabled": True,
            "command": ["npx", "-y", "chrome-devtools-mcp"],
            "environment": {"PATH": "/usr/bin"},
        }

    def test_handles_entry_without_env(self, tmp_path, monkeypatch):
        monkeypatch.setattr(opencode_mod, "WORKSPACE_ROOT", tmp_path)
        _, content = OpencodeExporter().export(
            {"mcpServers": {"a": {"command": "x"}}}
        )
        out = json.loads(content)
        assert out["mcp"]["a"] == {
            "type": "local",
            "enabled": True,
            "command": ["x"],
            "environment": {},
        }
