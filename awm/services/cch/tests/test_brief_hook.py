"""The SessionStart hook: the scope's brief (scopes.md R143) and the parent link of a clear (sessions.md R13)."""

from __future__ import annotations

import importlib.util
import io
import json
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parents[1] / "hooks" / "brief_on_start.py"


@pytest.fixture
def hook():
    spec = importlib.util.spec_from_file_location("awm_brief_hook", HOOK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def tree(tmp_path):
    wt = tmp_path / "ws" / "projects" / "awm" / "redux" / "harness"
    (wt / "src").mkdir(parents=True)
    (wt / ".git").write_text("gitdir: x")
    (tmp_path / "ws" / "projects" / "awm" / ".bare").mkdir()
    return wt


def test_the_scope_is_the_worktree_that_holds_the_folder(hook, tree):
    assert hook.scope_of(str(tree / "src")) == ("awm", "redux/harness")
    assert hook.scope_of(str(tree)) == ("awm", "redux/harness")
    assert hook.scope_of(str(tree.parents[1] / ".bare")) is None
    assert hook.scope_of(str(tree.parents[3])) is None


def _start(hook, monkeypatch, capsys, cwd, text):
    monkeypatch.setattr(hook, "brief", lambda p, s: text)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"cwd": cwd, "source": "startup"})))
    hook.main()
    return capsys.readouterr().out


def test_a_session_in_a_scope_gets_its_brief(hook, tree, monkeypatch, capsys):
    line = "The brief of awm/redux/harness: For the harness lane."
    out = json.loads(_start(hook, monkeypatch, capsys, str(tree), line))
    assert out["hookSpecificOutput"] == {
        "hookEventName": "SessionStart",
        "additionalContext": line}


def test_no_brief_and_no_scope_add_nothing(hook, tree, monkeypatch, capsys):
    assert _start(hook, monkeypatch, capsys, str(tree), "") == ""
    assert _start(hook, monkeypatch, capsys, "/tmp", "never read") == ""


def test_a_gateway_that_does_not_answer_adds_nothing(hook, monkeypatch):
    monkeypatch.setenv("AWM_HUB_URL", "http://127.0.0.1:9")
    assert hook.brief("awm", "s") == ""


def _payload(monkeypatch, source, session_id):
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(
        {"cwd": "/tmp", "source": source, "session_id": session_id})))


def test_a_clear_reports_the_id_the_process_had_before(hook, monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(hook, "claude_pid", __import__("os").getpid)
    reported = []
    monkeypatch.setattr(hook, "report_clear", lambda new, old: reported.append((new, old)))
    _payload(monkeypatch, "startup", "old-id")
    hook.main()
    _payload(monkeypatch, "compact", "old-id")
    hook.main()
    assert reported == []
    _payload(monkeypatch, "clear", "new-id")
    hook.main()
    assert reported == [("new-id", "old-id")]


def test_a_clear_with_no_earlier_id_reports_nothing(hook, monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(hook, "claude_pid", __import__("os").getpid)
    reported = []
    monkeypatch.setattr(hook, "report_clear", lambda new, old: reported.append((new, old)))
    _payload(monkeypatch, "clear", "new-id")
    hook.main()
    assert reported == []


def test_the_claude_process_is_the_ancestor_with_a_session_record(hook, monkeypatch, tmp_path):
    import os
    records = tmp_path / "claude" / "sessions"
    records.mkdir(parents=True)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    parent = os.getppid()
    assert hook.claude_pid() is None
    (records / f"{parent}.json").write_text("{}")
    assert hook.claude_pid() == parent
