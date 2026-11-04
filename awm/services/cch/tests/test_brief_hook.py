"""The SessionStart hook that hands a session its scope's brief (scopes.md R143)."""

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
    out = json.loads(_start(hook, monkeypatch, capsys, str(tree), "For the harness lane."))
    assert out["hookSpecificOutput"] == {
        "hookEventName": "SessionStart",
        "additionalContext": "The brief of awm/redux/harness: For the harness lane."}


def test_no_brief_and_no_scope_add_nothing(hook, tree, monkeypatch, capsys):
    assert _start(hook, monkeypatch, capsys, str(tree), "") == ""
    assert _start(hook, monkeypatch, capsys, "/tmp", "never read") == ""


def test_a_gateway_that_does_not_answer_adds_nothing(hook, monkeypatch):
    monkeypatch.setenv("AWM_HUB_URL", "http://127.0.0.1:9")
    assert hook.brief("awm", "s") == ""
