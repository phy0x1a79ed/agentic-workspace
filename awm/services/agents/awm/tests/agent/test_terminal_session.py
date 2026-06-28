"""Unit tests for the interactive tmux terminal session resolver.

The full PTY relay (`terminal_session`) is integration-level — it spawns a real
`tmux attach` against a live agent session and is exercised by the live-hub
harness. Here we cover the pure resolution logic that decides whether a relay
can even start: which agent the init identity names, and whether it runs under
the claude-tmux harness (so it has a pane to attach)."""
from __future__ import annotations

from awm.agents import terminal_session as ts
from awm.agents import agent_instances as ai


class _FakeInstance:
    def __init__(self, tmux_session=None):
        self.tmux_session = tmux_session


def test_init_requires_identity():
    sess, err = ts._resolve_tmux_session({})
    assert sess is None
    assert "init requires" in err


def test_invalid_session_id():
    sess, err = ts._resolve_tmux_session({"session_id": "nope"})
    assert sess is None
    assert "invalid session_id" in err


def test_no_live_session(monkeypatch):
    monkeypatch.setattr(ai, "get_session_by_scope", lambda p, s: None)
    sess, err = ts._resolve_tmux_session({"project": "p", "scope": "s"})
    assert sess is None
    assert "no live agent session" in err


def test_non_tmux_agent_has_no_terminal(monkeypatch):
    # A headless claude agent: no tmux_session → nothing to attach.
    monkeypatch.setattr(ai, "get_session_by_scope",
                        lambda p, s: _FakeInstance(tmux_session=None))
    sess, err = ts._resolve_tmux_session({"project": "p", "scope": "s"})
    assert sess is None
    assert "claude-tmux harness" in err


def test_resolves_tmux_session_by_scope(monkeypatch):
    monkeypatch.setattr(ai, "get_session_by_scope",
                        lambda p, s: _FakeInstance(tmux_session="awm-7-s"))
    sess, err = ts._resolve_tmux_session({"project": "p", "scope": "s"})
    assert err is None
    assert sess == "awm-7-s"


def test_resolves_tmux_session_by_session_id(monkeypatch):
    monkeypatch.setattr(ai, "get_session",
                        lambda sid: _FakeInstance(tmux_session="awm-9-x"))
    sess, err = ts._resolve_tmux_session({"session_id": "9"})
    assert err is None
    assert sess == "awm-9-x"


def test_terminal_registered_in_manifest():
    from awm.agents.hub_adapter import API_MANIFEST
    kinds = {s["kind"] for s in API_MANIFEST["sessions"]}
    assert "terminal" in kinds
