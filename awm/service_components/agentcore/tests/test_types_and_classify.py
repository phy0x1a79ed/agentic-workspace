"""Unit tests: config, event shape, and the per-harness classification — no
subprocess required."""

from __future__ import annotations

import pytest

from awm.agentcore import AgentConfig, AgentEvent
from awm.agentcore.claude_backend import build_claude_argv, _classify
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


# ---- claude argv ----

def test_claude_argv_full_perms():
    argv = build_claude_argv(AgentConfig(harness="claude", permissions="full"))
    assert "--permission-mode=bypassPermissions" in argv
    assert "--output-format=stream-json" in argv


def test_claude_argv_default_perms():
    argv = build_claude_argv(
        AgentConfig(harness="claude", permissions="default")
    )
    assert "--permission-mode=default" in argv


def test_claude_argv_model_effort_resume():
    argv = build_claude_argv(AgentConfig(
        harness="claude", model="opus", params={"effort": "high"},
        resume_id="sess-123",
    ))
    assert "--model" in argv and "opus" in argv
    assert "--effort" in argv and "high" in argv
    assert "--resume" in argv and "sess-123" in argv


def test_claude_argv_bad_effort_raises():
    with pytest.raises(ValueError):
        build_claude_argv(AgentConfig(harness="claude", params={"effort": "x"}))


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
