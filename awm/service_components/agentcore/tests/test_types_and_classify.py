"""Unit tests: config, event shape, and the per-harness classification — no
subprocess required."""

from __future__ import annotations

import pytest

import json

from awm.agentcore import AgentConfig, AgentEvent
from awm.agentcore import _mcp as mcp_mod
from awm.agentcore.claude_backend import _classify
from awm.agentcore.opencode_backend import _classify_parts, _extract_json_object


# ---- AgentConfig / AgentEvent ----

def test_config_defaults():
    cfg = AgentConfig(harness="claude")
    assert cfg.mode == "live"
    assert cfg.permissions == "full"
    assert cfg.params == {}


def test_event_has_id_and_ts():
    a = AgentEvent(kind="message", text="hi")
    b = AgentEvent(kind="message", text="hi")
    assert a.id != b.id  # ids are unique (the dedupe key)
    assert a.ts > 0
    assert a.to_dict()["kind"] == "message"


# NOTE: claude argv is the interactive tmux argv now (the headless --print
# backend was retired) — covered by tests/test_tmux_backend.py.


# ---- harness-owned MCP synthesis ----

def test_no_awm_server_without_hub_port(tmp_path):
    # Without awm_port the harness attaches no awm server (and writes no file).
    cfg = AgentConfig(harness="claude", workdir=str(tmp_path))
    assert mcp_mod.write_claude_mcp_config(cfg) is None
    assert not (tmp_path / ".awm" / "spawn-mcp.json").exists()


def test_claude_synthesizes_awm_server_with_identity(tmp_path, monkeypatch):
    monkeypatch.setattr(mcp_mod, "_resolve_awm_mcp", lambda: "/fake/awm-mcp")
    cfg = AgentConfig(
        harness="claude", workdir=str(tmp_path),
        awm_workspace="/the/ws", awm_port="7821", placement_as="p/leaf-1",
    )
    path = mcp_mod.write_claude_mcp_config(cfg)
    assert path == str(tmp_path / ".awm" / "spawn-mcp.json")
    out = json.loads(open(path).read())
    awm = out["mcpServers"]["awm"]
    assert awm["command"] == "/fake/awm-mcp"
    assert awm["env"] == {
        "AWM_WORKSPACE": "/the/ws", "AWM_PORT": "7821", "AWM_AS": "p/leaf-1",
    }


def test_resolve_awm_mcp_prefers_in_env_sibling(tmp_path, monkeypatch):
    # P1: the awm-mcp command must be the in-env console script (direct,
    # unbuffered stdio), NOT a PATH-searched ~/.local/bin shim that wraps
    # `mamba run` and buffers the MCP handshake. Prefer the binary next to the
    # running interpreter.
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "python").write_text("")
    sibling = bindir / "awm-mcp"
    sibling.write_text("")
    monkeypatch.setattr(mcp_mod.sys, "executable", str(bindir / "python"))
    monkeypatch.setattr(
        mcp_mod, "resolve_bin",
        lambda name: (_ for _ in ()).throw(AssertionError("should not fall back")))
    assert mcp_mod._resolve_awm_mcp() == str(sibling)


def test_resolve_awm_mcp_falls_back_when_no_sibling(tmp_path, monkeypatch):
    # No in-env sibling → fall back to the PATH search (resolve_bin).
    bindir = tmp_path / "bin"
    bindir.mkdir()
    monkeypatch.setattr(mcp_mod.sys, "executable", str(bindir / "python"))
    monkeypatch.setattr(mcp_mod, "resolve_bin", lambda name: f"/fallback/{name}")
    assert mcp_mod._resolve_awm_mcp() == "/fallback/awm-mcp"


def test_conversational_has_no_awm_as(tmp_path, monkeypatch):
    monkeypatch.setattr(mcp_mod, "_resolve_awm_mcp", lambda: "/fake/awm-mcp")
    cfg = AgentConfig(
        harness="claude", workdir=str(tmp_path),
        awm_workspace="/the/ws", awm_port="7821",  # no placement_as
    )
    out = json.loads(open(mcp_mod.write_claude_mcp_config(cfg)).read())
    assert "AWM_AS" not in out["mcpServers"]["awm"]["env"]


def test_opencode_synthesizes_awm_server(tmp_path, monkeypatch):
    monkeypatch.setattr(mcp_mod, "_resolve_awm_mcp", lambda: "/fake/awm-mcp")
    cfg = AgentConfig(
        harness="opencode", workdir=str(tmp_path),
        awm_workspace="/the/ws", awm_port="7821", placement_as="p/leaf-1",
    )
    path = mcp_mod.write_opencode_mcp_config(cfg)
    assert path == str(tmp_path / ".awm" / "mcp-opencode.json")
    out = json.loads(open(path).read())
    awm = out["mcp"]["awm"]
    assert awm["type"] == "local"
    assert awm["command"] == ["/fake/awm-mcp"]
    assert awm["environment"]["AWM_AS"] == "p/leaf-1"


# ---- claude classify ----

def test_classify_init_status():
    evts = _classify({
        "type": "system", "subtype": "init",
        "session_id": "abc", "model": "claude-opus",
        "slash_commands": ["/compact"],
    })
    assert len(evts) == 1
    assert evts[0].kind == "status"
    assert evts[0].data["session_id"] == "abc"


def test_classify_assistant_text_and_tool():
    evts = _classify({
        "type": "assistant",
        "message": {"content": [
            {"type": "text", "text": "hello"},
            {"type": "tool_use", "name": "Bash", "id": "t1",
             "input": {"command": "ls"}},
        ]},
    })
    kinds = [e.kind for e in evts]
    assert kinds == ["message", "tool_use"]
    assert evts[1].data["name"] == "Bash"
    assert evts[1].data["input"] == {"command": "ls"}


def test_classify_tool_result():
    evts = _classify({
        "type": "user",
        "message": {"content": [
            {"type": "tool_result", "tool_use_id": "t1",
             "content": "file1\nfile2"},
        ]},
    })
    assert evts[0].kind == "tool_result"
    assert "file1" in evts[0].text


def test_classify_partial():
    evts = _classify({
        "type": "stream_event",
        "event": {"type": "content_block_delta", "index": 0,
                  "delta": {"type": "text_delta", "text": "he"}},
    })
    assert evts[0].kind == "partial"
    assert evts[0].text == "he"


def test_classify_partial_stamps_message_id():
    # The streaming message id (tracked off message_start) is threaded in as
    # cur_msg_id and stamped onto each partial so the fold coalesces a turn.
    evts = _classify(
        {"type": "stream_event",
         "event": {"type": "content_block_delta", "index": 0,
                   "delta": {"type": "text_delta", "text": "he"}}},
        cur_msg_id="msg_abc",
    )
    assert evts[0].data["message_id"] == "msg_abc"


def test_classify_assistant_stamps_message_id():
    # Both the text message and a tool_use under one assistant message carry the
    # same message.id read straight off the parsed frame.
    evts = _classify({
        "type": "assistant",
        "message": {"id": "msg_xyz", "content": [
            {"type": "text", "text": "hello"},
            {"type": "tool_use", "name": "Bash", "id": "t1",
             "input": {"command": "ls"}},
        ]},
    })
    assert evts[0].kind == "message"
    assert evts[0].data["message_id"] == "msg_xyz"
    assert evts[1].kind == "tool_use"
    assert evts[1].data["message_id"] == "msg_xyz"
    # tool_use's own block id is distinct from the message id.
    assert evts[1].data["id"] == "t1"


def test_classify_result_ok_and_error():
    ok = _classify({"type": "result", "result": "done", "usage": {}})
    assert ok[0].kind == "result"
    assert ok[0].text == "done"
    err = _classify({"type": "result", "is_error": True, "result": "boom"})
    assert err[0].kind == "error"
    assert "boom" in err[0].text


# ---- opencode classify ----

def test_classify_opencode_parts():
    evts = _classify_parts(
        parts=[
            {"type": "step-start"},
            {"type": "reasoning", "text": "thinking..."},
            {"type": "text", "text": "the answer"},
            {"type": "tool", "tool": "read", "state": {
                "input": {"path": "x"}, "status": "completed", "output": "ok"}},
        ],
        info={},
    )
    kinds = [e.kind for e in evts]
    assert "status" in kinds
    assert "partial" in kinds
    assert "message" in kinds
    assert "tool_use" in kinds
    assert "tool_result" in kinds
    msg = next(e for e in evts if e.kind == "message")
    assert msg.text == "the answer"


def test_classify_opencode_error():
    evts = _classify_parts(parts=[], info={"error": "model exploded"})
    assert evts[0].kind == "error"
    assert "exploded" in evts[0].text


# ---- json extraction (schema'd one-shot path) ----

def test_extract_json_plain():
    assert _extract_json_object('{"a": 1}') == {"a": 1}


def test_extract_json_fenced():
    assert _extract_json_object('```json\n{"a": 1}\n```') == {"a": 1}


def test_extract_json_embedded_in_prose():
    txt = 'Sure! Here is the result: {"ok": true, "n": 2} — done.'
    assert _extract_json_object(txt) == {"ok": True, "n": 2}


def test_extract_json_none():
    assert _extract_json_object("no json here") is None
