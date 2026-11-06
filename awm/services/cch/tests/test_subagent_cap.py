"""The subagent cap hook: at most 5 subagents at a time below trusted (channels.md R125 item 7)."""

from __future__ import annotations

import importlib.util
import io
import json
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parents[1] / "hooks" / "subagent_cap.py"


@pytest.fixture
def hook(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    spec = importlib.util.spec_from_file_location("awm_subagent_cap", HOOK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _fire(hook, monkeypatch, capsys, event, **extra):
    payload = {"session_id": "s1", "hook_event_name": event, **extra}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    hook.main()
    out = capsys.readouterr().out
    return json.loads(out)["hookSpecificOutput"] if out else None


def _agent(hook, monkeypatch, capsys):
    return _fire(hook, monkeypatch, capsys, "PreToolUse", tool_name="Agent", tool_input={})


def test_a_cooperative_session_gets_five_at_a_time(hook, monkeypatch, capsys):
    monkeypatch.setattr(hook, "level", lambda: "cooperative")
    for _ in range(5):
        assert _agent(hook, monkeypatch, capsys) is None
        _fire(hook, monkeypatch, capsys, "SubagentStart")
    denied = _agent(hook, monkeypatch, capsys)
    assert denied["permissionDecision"] == "deny" and "at most 5" in denied["permissionDecisionReason"]
    _fire(hook, monkeypatch, capsys, "SubagentStop")
    assert _agent(hook, monkeypatch, capsys) is None


def test_calls_sent_together_count_each_other(hook, monkeypatch, capsys):
    """Five Agent calls in one turn reserve five slots before any subagent starts."""
    monkeypatch.setattr(hook, "level", lambda: "untrusted")
    assert all(_agent(hook, monkeypatch, capsys) is None for _ in range(5))
    assert _agent(hook, monkeypatch, capsys)["permissionDecision"] == "deny"


def test_a_reservation_that_never_starts_lapses(hook, monkeypatch, capsys):
    monkeypatch.setattr(hook, "level", lambda: "cooperative")
    clock = [1000.0]
    monkeypatch.setattr(hook.time, "time", lambda: clock[0])
    for _ in range(5):
        _agent(hook, monkeypatch, capsys)
    assert _agent(hook, monkeypatch, capsys) is not None
    clock[0] += hook.RESERVE_S + 1
    assert _agent(hook, monkeypatch, capsys) is None


def test_a_stop_frees_its_own_agent(hook, monkeypatch, capsys):
    monkeypatch.setattr(hook, "level", lambda: "cooperative")
    for i in range(5):
        _agent(hook, monkeypatch, capsys)
        _fire(hook, monkeypatch, capsys, "SubagentStart", agent_id=f"a{i}")
    _fire(hook, monkeypatch, capsys, "SubagentStop", agent_id="a3")
    _fire(hook, monkeypatch, capsys, "SubagentStop", agent_id="a3")
    assert _agent(hook, monkeypatch, capsys) is None
    assert _agent(hook, monkeypatch, capsys) is not None, "a repeated stop frees no second slot"


def test_a_subagent_whose_stop_never_comes_lapses(hook, monkeypatch, capsys):
    monkeypatch.setattr(hook, "level", lambda: "cooperative")
    clock = [1000.0]
    monkeypatch.setattr(hook.time, "time", lambda: clock[0])
    for i in range(5):
        _agent(hook, monkeypatch, capsys)
        _fire(hook, monkeypatch, capsys, "SubagentStart", agent_id=f"a{i}")
    clock[0] += hook.RESERVE_S + 1
    assert _agent(hook, monkeypatch, capsys) is not None
    clock[0] += hook.RUNNING_S
    assert _agent(hook, monkeypatch, capsys) is None


def test_a_trusted_session_is_not_capped(hook, monkeypatch, capsys):
    monkeypatch.setattr(hook, "level", lambda: "trusted")
    for _ in range(7):
        assert _agent(hook, monkeypatch, capsys) is None
        _fire(hook, monkeypatch, capsys, "SubagentStart")


def test_other_tools_and_bad_input_pass(hook, monkeypatch, capsys):
    monkeypatch.setattr(hook, "level", lambda: "untrusted")
    for _ in range(6):
        assert _fire(hook, monkeypatch, capsys, "PreToolUse", tool_name="Bash") is None
    monkeypatch.setattr("sys.stdin", io.StringIO("not json"))
    hook.main()
    assert capsys.readouterr().out == ""


def test_an_unreachable_gateway_reads_as_untrusted(hook, monkeypatch):
    monkeypatch.setenv("AWM_HUB_URL", "http://127.0.0.1:9")
    assert hook.level() == "untrusted"
