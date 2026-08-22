"""The plan-approval hook's guards, and what it does when the call fails.

The hook is loaded by path: it ships as a standalone stdlib script (Claude Code
runs it under `python3 -S`, outside this package), so importing it the way the
harness does is part of what these tests check.
"""
from __future__ import annotations

import importlib.util
import io
import json
import pathlib

import pytest

pytestmark = [pytest.mark.smoke]

HOOK = pathlib.Path(__file__).resolve().parents[1] / "hooks" / "plan_mode_hook.py"


def _load():
    spec = importlib.util.spec_from_file_location("awm_plan_mode_hook", HOOK)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


hook = _load()


def _payload(**over) -> dict:
    base = {"hook_event_name": "PostToolUse", "tool_name": "ExitPlanMode",
            "session_id": "sid-1", "permission_mode": "acceptEdits",
            "tool_response": {"plan": "# a plan", "isAgent": False}}
    base.update(over)
    return base


def test_an_approved_plan_is_acted_on():
    assert hook.should_act(_payload())
    assert hook.should_act(_payload(permission_mode="auto"))
    assert hook.should_act(_payload(permission_mode="default"))


def test_a_rejected_plan_is_left_alone():
    # A rejection does not reach a PostToolUse hook at all today; if that ever
    # changes, cycling the mode would kick the session *out* of plan mode, which
    # is worse than doing nothing.
    assert not hook.should_act(_payload(tool_response={"status": "denied"}))
    assert not hook.should_act(_payload(tool_response="user rejected"))
    assert not hook.should_act(_payload(tool_response=None))


def test_a_session_already_in_bypass_is_not_touched():
    assert not hook.should_act(_payload(permission_mode="bypassPermissions"))


def test_a_session_still_reading_as_plan_stands_down():
    # ExitPlanMode restores the pre-plan mode inside its own call, so we never
    # see "plan" — seeing it means something unmodelled happened.
    assert not hook.should_act(_payload(permission_mode="plan"))


def test_other_tools_are_ignored():
    assert not hook.should_act(_payload(tool_name="Bash"))


def test_the_call_carries_the_hooks_own_pid_and_expected_session(monkeypatch):
    sent = {}

    class FakeResponse:
        def read(self):
            return json.dumps({"result": {"ok": True, "mode": "bypassPermissions"}}).encode()

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def fake_urlopen(req, timeout=None):
        sent["url"] = req.full_url
        sent["headers"] = dict(req.headers)
        sent["body"] = json.loads(req.data.decode())
        return FakeResponse()

    monkeypatch.setattr(hook.urllib.request, "urlopen", fake_urlopen)
    assert hook.call_mode("sid-1") == {"ok": True, "mode": "bypassPermissions"}
    assert sent["url"].endswith("/invoke")
    # The gateway resolves this pid to the session that owns it; without the
    # header `_caller_pid` is stripped and the call refuses.
    assert sent["headers"]["X-awm-caller-pid"] == str(hook.os.getpid())
    assert sent["body"] == {"name": "reflection_mode",
                            "args": {"expect_session": "sid-1"}}


def _no_wait(monkeypatch):
    monkeypatch.setattr(hook.time, "sleep", lambda _s: None)


def test_a_covered_footer_is_retried(monkeypatch):
    # On tmux an unreadable footer is a modal, and whatever covers it right after
    # an approval is transient.
    calls = []
    results = [{"ok": False, "mode": "unknown", "hosting": "tmux"}, {"ok": True}]
    monkeypatch.setattr(hook, "call_mode",
                        lambda sid: calls.append(sid) or results[len(calls) - 1])
    monkeypatch.setattr(hook, "note", lambda msg: pytest.fail(f"logged: {msg}"))
    _no_wait(monkeypatch)
    hook.ensure_bypass("sid-1")
    assert calls == ["sid-1", "sid-1"]


def test_a_blind_lane_is_not_retried(monkeypatch):
    # A background session shows an append-only pty stream, and a footer paint
    # that is not in it does not arrive by waiting — measured live, with the mode
    # changing exactly as asked while every read came back unknown. Retrying
    # would stall the turn for nothing.
    calls, logged = [], []
    monkeypatch.setattr(hook, "call_mode", lambda sid: calls.append(sid) or {
        "ok": False, "mode": "unknown", "hosting": "background",
        "error": "cannot see this session's permission-mode indicator"})
    monkeypatch.setattr(hook, "note", logged.append)
    _no_wait(monkeypatch)
    hook.ensure_bypass("sid-1")
    assert len(calls) == 1
    assert len(logged) == 1


def test_a_settled_refusal_is_logged_once_and_not_retried(monkeypatch):
    calls, logged = [], []
    monkeypatch.setattr(hook, "call_mode", lambda sid: calls.append(sid) or {
        "ok": False, "mode": "auto",
        "error": "this session's permission-mode cycle never offers bypass"})
    monkeypatch.setattr(hook, "note", logged.append)
    _no_wait(monkeypatch)
    hook.ensure_bypass("sid-1")
    assert len(calls) == 1
    assert "never offers bypass" in logged[0]


def test_an_unreachable_gateway_is_retried_then_logged(monkeypatch):
    calls, logged = [], []

    def boom(sid):
        calls.append(sid)
        raise OSError("connection refused")

    monkeypatch.setattr(hook, "call_mode", boom)
    monkeypatch.setattr(hook, "note", logged.append)
    _no_wait(monkeypatch)
    hook.ensure_bypass("sid-1")
    assert len(calls) > 1, "a restart window must be retried across"
    assert len(logged) == 1 and "connection refused" in logged[0]


def test_main_writes_nothing_to_stdout(monkeypatch, capsys):
    # A PostToolUse hook's stdout is parsed as a JSON control object.
    monkeypatch.setattr(hook.sys, "stdin", io.StringIO(json.dumps(_payload())))
    monkeypatch.setattr(hook, "ensure_bypass", lambda sid: None)
    _no_wait(monkeypatch)
    assert hook.main() == 0
    assert capsys.readouterr().out == ""


def test_main_survives_junk_on_stdin(monkeypatch):
    monkeypatch.setattr(hook.sys, "stdin", io.StringIO("not json"))
    assert hook.main() == 0
