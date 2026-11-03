"""A worker swap during `/compact` must not lose the compaction or its resume.

Claude Code's daemon swaps an outdated background worker for a new process in
place: same session id, new pid. A swap while `/compact` runs loses the
compaction and every line queued behind it (compaction-survey.md, 2026-11-03).
These tests drive the watcher through such swaps with a scripted transcript.
"""
from __future__ import annotations

import time
from contextlib import contextmanager

import pytest

from awm.cch import inject, pending, session_target, watcher

from _cch_events import lost_events

LANE = session_target.TmuxLane(pane="%7", session_id="sid-1", repl_pid=4242,
                               name="test")
OLD, NEW = 4242, 5151


class Script:
    """One session's transcript and processes, moved along by what gets typed."""

    def __init__(self):
        self.alive = {OLD: "999"}
        self.seen: list[str] = []
        self.landed = False
        self.taken = False
        self.compacted = False
        self.on_write = lambda text: None

    def proc_start(self, pid):
        return self.alive.get(pid)

    def swap(self):
        self.alive = {NEW: "777"}


class Tail:
    def __init__(self, script: Script):
        self.s = script
        self.repl_pid = OLD

    def poll(self):
        return True

    def watch(self, _text):
        pass

    def expect(self, _text, **_kw):
        pass

    def rebind(self, pid):
        self.repl_pid = pid

    def tool_call_in_flight(self):
        return False

    def landed(self, _text, *, since_ms=None):
        return self.s.landed

    def consumed(self, _text):
        return self.s.taken

    def queued(self, _text):
        return False

    def started(self, _text, *, since_ms=None):
        return True

    def compacted(self, *, since_ms=None):
        return self.s.compacted

    def taken_up(self, _text, *, since_ms):
        return self.s.taken


class Writer:
    label = "the fake lane"

    def __init__(self, script: Script):
        self.s = script
        self.pending = ""

    def clear(self):
        pass

    def write(self, text):
        self.pending = text

    def check_not_rejected(self):
        pass

    def commit(self):
        self.s.seen.append(self.pending)
        self.s.on_write(self.pending)


@pytest.fixture
def script(monkeypatch, tmp_path):
    s = Script()
    monkeypatch.setattr(pending, "PENDING_DIR", tmp_path / "pending")

    @contextmanager
    def open_lane(_lane, **_kw):
        yield Writer(s)

    monkeypatch.setattr(inject, "_open_lane", open_lane)
    monkeypatch.setattr(session_target, "resolve", lambda pid, **kw: LANE)
    monkeypatch.setattr(session_target, "_proc_start", s.proc_start)
    monkeypatch.setattr(
        session_target, "successor",
        lambda sid, old=None, proc_start=None: next(
            ((p, st) for p, st in s.alive.items() if p != old and sid == "sid-1"),
            None))
    monkeypatch.setattr(inject, "ROUND_BACKOFF_S", (0.0,))
    monkeypatch.setattr(inject, "SUCCESSOR_WAIT_S", 0.0)
    monkeypatch.setattr(inject, "VERIFY_WAIT_S", 0.0)
    return s


def _promise(**over) -> pending.Pending:
    base = dict(repl_pid=OLD, proc_start="999", session_id="sid-1",
                text="/compact", followup="resume",
                injected_at_ms=int(time.time() * 1000) - 1000, name="test",
                hosting="tmux", ticket="t-1")
    base.update(over)
    return pending.Pending(**base)


def _run(item, script, outcomes):
    remaining = list(outcomes)

    def fake_await(*_a, **_kw):
        return remaining.pop(0) if remaining else watcher.STARTED_OUTCOME

    pending.record(item)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(watcher, "await_completion", fake_await)
        inject._await_and_resume(item, tail=Tail(script),
                                 read_status=lambda pid: None,
                                 sleep=lambda _s: None)


def test_a_swap_after_the_resume_was_typed_reissues_the_compaction(script):
    # The altair incident: the resume was enqueued, then the worker was swapped
    # mid-compaction. The new worker never compacted and never saw the resume.
    resumes = []

    def on_write(text):
        if text == "resume":
            resumes.append(text)
            script.landed = True
            if len(resumes) == 1:
                script.swap()
            else:
                script.taken = script.compacted = True
        elif text == "/compact":
            script.landed = False

    script.on_write = on_write
    _run(_promise(), script, [watcher.STARTED_OUTCOME])
    assert script.seen == ["resume", "/compact", "resume"]
    assert pending.load_all() == []
    assert lost_events() == []


def test_the_promise_stays_on_disk_until_the_resume_is_taken_up(script):
    seen_on_disk = []

    def on_write(text):
        script.landed = True
        seen_on_disk.append([p.phase for p in pending.load_all()])

    script.on_write = on_write
    calls = {"n": 0}

    def taken(*_a, **_kw):
        calls["n"] += 1
        return calls["n"] > 3

    tail = Tail(script)
    tail.taken_up = taken
    script.compacted = True
    item = _promise()
    pending.record(item)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(watcher, "await_completion",
                   lambda *a, **k: watcher.STARTED_OUTCOME)
        inject._await_and_resume(item, tail=tail, read_status=lambda pid: None,
                                 sleep=lambda _s: None)
    assert seen_on_disk == [[pending.RESUME_SENT]]
    assert calls["n"] == 4
    assert pending.load_all() == []


def test_a_swap_before_the_command_ran_types_it_again(script):
    def on_write(text):
        if text == "resume":
            script.landed = script.taken = script.compacted = True

    script.on_write = on_write
    script.swap()
    _run(_promise(), script, [watcher.VANISHED, watcher.STARTED_OUTCOME])
    assert script.seen == ["/compact", "resume"]
    assert pending.load_all() == []


def test_a_session_with_no_successor_is_lost_loudly(script):
    script.alive = {}
    _run(_promise(), script, [watcher.VANISHED])
    assert script.seen == []
    [entry] = lost_events()
    assert entry["command"] == "/compact"
    assert "no new process" in entry["reason"]
    assert pending.load_all() == []


def test_a_replay_of_a_sent_resume_does_not_type_it_again(script):
    script.taken = script.compacted = True
    _run(_promise(phase=pending.RESUME_SENT), script, [])
    assert script.seen == []
    assert pending.load_all() == []


def test_a_resume_taken_up_without_a_boundary_is_lost_and_the_session_told(script):
    def on_write(text):
        if text == "resume":
            script.landed = script.taken = True

    script.on_write = on_write
    _run(_promise(), script, [watcher.STARTED_OUTCOME])
    assert script.seen[0] == "resume"
    assert script.seen[1].startswith("[awm reflection] Your `/compact`")
    [entry] = lost_events()
    assert "compact_boundary" in entry["reason"]


def test_swaps_are_bounded(script):
    def on_write(text):
        script.landed = text == "resume"
        if text == "resume":
            pid = next(iter(script.alive))
            script.alive = {pid + 1: "777"}

    script.on_write = on_write
    _run(_promise(), script, [])
    assert script.seen.count("/compact") == inject.MAX_REISSUES
    [entry] = lost_events()
    assert "re-issues" in entry["reason"]


def test_load_all_moves_a_promise_to_the_new_worker(script):
    pending.record(_promise())
    script.swap()
    [item] = pending.load_all()
    assert (item.repl_pid, item.proc_start) == (NEW, "777")
    assert pending.read(OLD) is None


def test_load_all_announces_a_promise_with_no_session_left(script):
    pending.record(_promise())
    script.alive = {}
    assert pending.load_all() == []
    [entry] = lost_events()
    assert entry["session_id"] == "ccsid1"


def test_a_lost_event_goes_to_reflection_with_the_sessions_folder(script, events_sent):
    from awm.cch import events
    events.lost(_promise(), "testing",
                cwd="/home/tony/agentic_workspace/projects/awm/redux-harness")
    [(service, fn, args)] = events_sent
    assert (service, fn) == ("reflection", "harness_event")
    assert args["kind"] == "lost" and args["reason"] == "testing"
    assert args["cwd"].endswith("projects/awm/redux-harness")
    assert args["ticket"] == "t-1"


def test_an_event_reflection_refuses_is_spooled_and_sent_again(script, monkeypatch):
    from awm.cch import events
    replies = [{"ok": False, "error": "down"}, {"ok": True}]
    got = []
    monkeypatch.setattr(events, "_call",
                        lambda s, f, a: (got.append(a), replies.pop(0))[1])
    assert events.done(_promise()) is False
    assert events.resend_unsent() == 1
    assert [a["kind"] for a in got] == ["done", "done"]
    assert events.resend_unsent() == 0
