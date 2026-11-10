"""The session helpers ask reflection and read its answer, never a harness's files."""
from __future__ import annotations

import pytest

import awm.gatewayclient as gc
from awm.gatewayclient import sessions

SID = "cc" + "a" * 32


def test_session_of_asks_reflection_with_the_pid(monkeypatch):
    seen = []
    monkeypatch.setattr(gc, "call_sync", lambda svc, fn, args, **kw: seen.append(
        (svc, fn, args)) or {"ok": True, "session_id": "cc1", "job": "abcd1234"})
    assert sessions.session_of(42)["job"] == "abcd1234"
    assert seen == [("reflection", "session_of", {"pid": 42})]


@pytest.mark.parametrize("pid", [None, "42", True, 0, -3])
def test_a_pid_that_is_no_pid_asks_nothing(monkeypatch, pid):
    monkeypatch.setattr(gc, "call_sync", lambda *a, **kw: pytest.fail("asked"))
    assert sessions.session_of(pid) is None


@pytest.mark.parametrize("reply", [{"ok": False, "reason": "no session"}, {"ok": True}, "x"])
def test_no_session_reads_as_none(monkeypatch, reply):
    monkeypatch.setattr(gc, "call_sync", lambda *a, **kw: reply)
    assert sessions.session_of(42) is None


def test_reflection_down_reads_as_none(monkeypatch):
    def down(*a, **kw):
        raise ConnectionError("refused")

    monkeypatch.setattr(gc, "call_sync", down)
    assert sessions.session_of(42) is None


def test_tell_raises_when_the_line_was_not_delivered(monkeypatch):
    monkeypatch.setattr(gc, "call_sync", lambda *a, **kw: {"ok": False, "reason": "a modal is open"})
    with pytest.raises(RuntimeError, match="a modal is open"):
        sessions.tell(SID, "hi")


def test_tell_messages_the_session(monkeypatch):
    seen = []
    monkeypatch.setattr(gc, "call_sync", lambda svc, fn, args, **kw: seen.append(
        (svc, fn, args)) or {"ok": True})
    sessions.tell(SID, "hi")
    assert seen == [("reflection", "message", {"to": {"id": SID}, "text": "hi"})]
