"""Tests for the MCP-config export framework + the opencode backend.

The framework reads canonical workspace ``.mcp.json`` and fans it out to
backend-specific config files. ``opencode`` translates the schema for the
per-scope workspace orientation config. (Spawned agents' own MCP config is
harness-owned now — synthesized in agentcore at spawn, not exported here.)
These tests cover the framework's plumbing (register dedup, report shape,
exporter isolation) and the opencode backend's invariants.
"""

from __future__ import annotations


import pytest
pytestmark = [pytest.mark.mcp, pytest.mark.slow, pytest.mark.subprocess]

import json
import os
from contextlib import contextmanager
from pathlib import Path

import pytest

import awm.gateway.exports.backends.opencode as opencode_mod
from awm.gateway.exports import mcp as mcp_mod
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
