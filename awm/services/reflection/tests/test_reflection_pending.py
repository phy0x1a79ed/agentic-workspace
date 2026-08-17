"""The promised resume must outlive the process that promised it.

The deferred follow-up is what makes a self-directed ``/compact`` safe, and until
these tests it existed only as a thread. The gateway drains and respawns its
services routinely, and the wait can run for minutes — so a restart in that
window took the watcher with it and left the session idle forever, with the
caller already told the resume was on its way. That is what is covered here: the
promise is written down before the watcher starts, replayed on boot, and only
ever replayed at the session that actually earned it.

None of this fakes a socket or a pane any more. Promising, waiting and replaying
became transport-free when the two backend watchers were folded into one, and a
test here that needed a transport would be a sign that they had come apart again.
"""
from __future__ import annotations

import logging
import time
from contextlib import contextmanager

import pytest

pytestmark = [pytest.mark.smoke]

from awm.reflection import inject, pending, session_target, watcher


LANE = session_target.TmuxLane(pane="%7", session_id="sid-1", repl_pid=4242,
                               name="test")


class Tail:
    """A transcript that answers whatever the test says it answers."""

    def __init__(self, in_flight=None, landed=None):
        self._in_flight = in_flight
        self._landed = landed

    def poll(self):
        return self._in_flight is not None

    def watch(self, _text):
        pass

    def tool_call_in_flight(self):
        return self._in_flight

    def landed(self, _text):
        return self._landed

    def consumed(self, _text):
        return self._landed

    def queued(self, _text):
        return False


def _promise(**over) -> pending.Pending:
    """A promise stamped *now*, for a session that is alive."""
    base = dict(repl_pid=4242, proc_start="999", session_id="sid-1",
                text="/compact", followup="resume",
                injected_at_ms=int(time.time() * 1000), name="test",
                hosting="tmux")
    base.update(over)
    return pending.Pending(**base)


class Writer:
    """A lane that accepts anything and shows what was written."""

    label = "the fake lane"

    def __init__(self, seen):
        self.seen = seen
        self._screen = ""

    def read_back(self):
        return self._screen

    def clear(self):
        self._screen = ""

    def write(self, text):
        self.seen.append(text)
        self._screen += text

    def commit(self):
        pass


@pytest.fixture(autouse=True)
def _pending_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(pending, "PENDING_DIR", tmp_path / "pending")
    yield tmp_path / "pending"


@pytest.fixture
def lane(monkeypatch):
    """Wire the sender to a fake lane and return the list of texts written."""
    seen: list[str] = []

    @contextmanager
    def open_lane(_lane, **_kw):
        yield Writer(seen)

    monkeypatch.setattr(inject, "_open_lane", open_lane)
    monkeypatch.setattr(session_target, "resolve", lambda pid, **kw: LANE)
    monkeypatch.setattr(session_target, "_proc_start", lambda pid: "999")
    return seen


# ---------------------------------------------------------------------------
# Recording the promise
# ---------------------------------------------------------------------------

def test_a_deferred_resume_is_written_down_before_the_watcher_starts(lane):
    # The ordering is the whole point: if the record were written by the watcher
    # thread, a restart could land between the paste and the first write and the
    # promise would exist nowhere at all.
    written = []
    inject.send("/compact", caller_pid=4242,
                spawn=lambda fn: written.append(pending.load_all()))
    assert len(written) == 1
    [item] = written[0]
    assert item.repl_pid == 4242
    assert item.text == "/compact"
    assert item.followup == "Continue with what you were doing."
    assert item.proc_start == "999"


def test_a_plain_prompt_promises_nothing(lane):
    inject.send("just a prompt", caller_pid=4242, spawn=lambda fn: None)
    assert pending.load_all() == []


def test_no_promise_survives_a_command_that_never_went_in(monkeypatch):
    # The record-and-spawn sits *below* the delivery, so a send that failed
    # leaves nothing behind for a resume to a command that was never injected.
    monkeypatch.setattr(session_target, "resolve", lambda pid, **kw: LANE)
    monkeypatch.setattr(inject, "_open_lane",
                        lambda *a, **k: (_ for _ in ()).throw(
                            inject.tmux_inject.TmuxError("pane is gone")))
    with pytest.raises(inject.DeliveryError):
        inject.send("/compact", caller_pid=4242, spawn=lambda fn: None)
    assert pending.load_all() == []


def test_the_promise_is_cleared_once_the_resume_is_delivered(lane, monkeypatch):
    monkeypatch.setattr(watcher, "await_completion",
                        lambda *a, **kw: watcher.SETTLED_OUTCOME)
    pending.record(_promise())
    inject._await_and_resume(_promise())
    assert lane == ["resume"]
    assert pending.load_all() == [], "a delivered resume must not be replayed"


def test_an_undeliverable_resume_stays_pending_and_shouts(lane, monkeypatch,
                                                          caplog):
    # The one state worth seeing was the one state `pending` could not show. The
    # promise is cleared before delivery on purpose — that is what stops a boot
    # replay re-delivering a resume that already landed — but when the delivery
    # then fails, a session is owed a resume nobody is waiting to give it. It
    # went out as a WARNING and vanished from disk; it is now an ERROR and stays.
    monkeypatch.setattr(watcher, "await_completion",
                        lambda *a, **kw: watcher.SETTLED_OUTCOME)
    monkeypatch.setattr(inject, "_open_lane",
                        lambda *a, **k: (_ for _ in ()).throw(
                            inject.tmux_inject.TmuxError("pane is gone")))
    pending.record(_promise())
    with caplog.at_level(logging.ERROR, logger="awm.reflection.inject"):
        inject._await_and_resume(_promise())
    assert [i.followup for i in pending.load_all()] == ["resume"]
    assert any("sit idle" in r.getMessage() for r in caplog.records)


def test_a_session_that_never_comes_free_keeps_its_promise_and_shouts(
        lane, monkeypatch, caplog):
    # The cap used to inject anyway, on the theory that a late resume beats none.
    # It does not: a resume typed into a session mid-turn goes into its queue and
    # runs whenever that turn ends, on whatever context is there by then. So the
    # promise is held, every round is recorded, and the give-up is an ERROR.
    monkeypatch.setattr(watcher, "await_completion",
                        lambda *a, **kw: watcher.TIMED_OUT)
    pending.record(_promise())
    with caplog.at_level(logging.ERROR, logger="awm.reflection.inject"):
        inject._await_and_resume(_promise())
    assert lane == [], "nothing may be typed into a session that is not free"
    [item] = pending.load_all()
    assert item.holds == inject.MAX_HOLD_ROUNDS
    assert "has not been free" in item.last_outcome
    assert any("gave up delivering" in r.getMessage() for r in caplog.records)


def test_a_busy_session_at_the_last_instant_holds_rather_than_types(
        lane, monkeypatch):
    # The watcher said settled, but a turn started in the gap between the wait
    # and the write. Holding the promise is only worth anything if the delivery
    # itself checks too.
    monkeypatch.setattr(watcher, "await_completion",
                        lambda *a, **kw: watcher.SETTLED_OUTCOME)
    monkeypatch.setattr(watcher, "read_status", lambda pid: ("busy", 9))
    pending.record(_promise())
    inject._await_and_resume(_promise())
    assert lane == []
    assert len(pending.load_all()) == 1


def test_a_delivery_the_session_never_showed_is_tried_again(lane, monkeypatch):
    # Verification is the half that was missing: a send used to be believed on
    # the strength of the keystrokes having gone out. The transcript is asked
    # whether they arrived, and a definite no puts the promise back.
    monkeypatch.setattr(watcher, "await_completion",
                        lambda *a, **kw: watcher.SETTLED_OUTCOME)
    monkeypatch.setattr(inject, "VERIFY_WAIT_S", 0.0)
    pending.record(_promise())
    inject._await_and_resume(_promise(), tail=Tail(in_flight=False, landed=False))
    assert lane == ["resume"] * inject.MAX_HOLD_ROUNDS
    [item] = pending.load_all()
    assert "never showed it arriving" in item.last_outcome


def test_an_unreadable_transcript_does_not_license_a_second_delivery(
        lane, monkeypatch):
    # "I could not read the transcript" is not proof that the resume was lost,
    # and typing the same resume in twice is worse than not verifying at all.
    monkeypatch.setattr(watcher, "await_completion",
                        lambda *a, **kw: watcher.SETTLED_OUTCOME)
    monkeypatch.setattr(inject, "VERIFY_WAIT_S", 0.0)
    pending.record(_promise())
    inject._await_and_resume(_promise(), tail=Tail(in_flight=None, landed=None))
    assert lane == ["resume"]
    assert pending.load_all() == []


def test_a_vanished_session_clears_its_promise_without_typing(lane, monkeypatch):
    monkeypatch.setattr(watcher, "await_completion",
                        lambda *a, **kw: watcher.VANISHED)
    pending.record(_promise())
    inject._await_and_resume(_promise())
    assert lane == [], "there is nobody left to resume"
    assert pending.load_all() == []


def test_a_long_held_promise_survives_while_its_session_lives(monkeypatch):
    # Behaviour change, and the point of holding a resume rather than forcing it
    # into a busy session: a promise held for an hour is this mechanism working,
    # not a stale record. There used to be a flat twenty-minute cutoff here, and
    # it reinstated the original bug — a session left idle forever — for any
    # session that stayed busy longer than that.
    monkeypatch.setattr(session_target, "_proc_start", lambda pid: "999")
    pending.record(_promise(injected_at_ms=0))
    kept = pending.load_all(now_ms=4 * 60 * 60 * 1000)
    assert [i.followup for i in kept] == ["resume"]


def test_a_promise_whose_session_is_gone_is_dropped(monkeypatch):
    # Liveness, not age, is what decides. The pid is the key, and a pid outlives
    # nothing.
    monkeypatch.setattr(session_target, "_proc_start", lambda pid: None)
    pending.record(_promise())
    assert pending.load_all() == []


def test_a_promise_on_a_recycled_pid_is_dropped(monkeypatch):
    monkeypatch.setattr(session_target, "_proc_start", lambda pid: "different")
    pending.record(_promise())
    assert pending.load_all() == []


def test_holding_a_promise_records_why_without_moving_its_clock():
    # `pending` has to be able to say what a held resume is waiting for. The
    # injection time must not move with it: that is the watcher's reference for
    # "the session reacted after this moment".
    item = _promise(injected_at_ms=1234)
    held = pending.held(item, "the session has not been free")
    assert held.holds == 1
    assert held.last_outcome == "the session has not been free"
    assert held.injected_at_ms == 1234
    assert pending.held(held, "again").holds == 2


def test_an_unreadable_record_is_discarded_not_carried_forever(_pending_dir):
    _pending_dir.mkdir(parents=True, exist_ok=True)
    junk = _pending_dir / "4242.json"
    junk.write_text("{not json")
    assert pending.load_all() == []
    assert not junk.exists()


# ---------------------------------------------------------------------------
# Replay on boot
# ---------------------------------------------------------------------------

def test_replay_re_arms_the_watcher_without_re_running_the_command(monkeypatch):
    # This is the bug: reflection was killed mid-wait by a gateway restart and
    # the session sat idle forever. Replay must pick the *wait* back up — and
    # must not paste `/compact` a second time.
    monkeypatch.setattr(session_target, "_proc_start", lambda pid: "999")
    pending.record(_promise())
    armed = []
    monkeypatch.setattr(inject, "resume_watch",
                        lambda item, **kw: armed.append(item))
    assert inject.replay_pending() == {"pending": 1, "resumed": 1}
    assert [i.text for i in armed] == ["/compact"]
    assert [i.followup for i in armed] == ["resume"]
    # Still owed until the re-armed watcher actually delivers it.
    assert len(pending.load_all()) == 1


def test_replay_carries_the_original_injection_time(monkeypatch):
    # The "has the session reacted yet?" test compares the session's status
    # timestamp against when the command went in. Re-stamping it at boot would
    # make a session that already finished compacting look like it never started
    # — and on this path the command ALWAYS ran before the restart, so it is the
    # one place that mistake is guaranteed rather than merely possible.
    monkeypatch.setattr(session_target, "_proc_start", lambda pid: "999")
    stamp = int(time.time() * 1000) - 60_000   # injected a minute before the restart
    pending.record(_promise(injected_at_ms=stamp))
    seen = []
    monkeypatch.setattr(watcher, "await_completion",
                        lambda pid, **kw: seen.append(kw["injected_at_ms"])
                        or watcher.VANISHED)
    monkeypatch.setattr(inject, "_default_spawn", lambda fn: fn())
    inject.replay_pending()
    assert seen == [stamp]


def test_replay_refuses_a_recycled_pid(monkeypatch):
    # A pid outlives nothing. If the number was reused, the record describes a
    # process that is gone and the resume would land in a stranger's prompt.
    monkeypatch.setattr(session_target, "_proc_start", lambda pid: "different")
    monkeypatch.setattr(inject, "resume_watch",
                        lambda item, **kw: pytest.fail("must not re-arm"))
    pending.record(_promise())
    assert inject.replay_pending() == {"pending": 0, "resumed": 0}
    assert pending.load_all() == []


def test_replay_drops_a_session_that_is_simply_gone(monkeypatch):
    monkeypatch.setattr(session_target, "_proc_start", lambda pid: None)
    pending.record(_promise())
    assert inject.replay_pending() == {"pending": 0, "resumed": 0}
    assert pending.load_all() == []


def test_replay_keeps_a_promise_it_cannot_currently_reach(monkeypatch):
    # Behaviour change, and deliberate. Replay used to resolve the lane at boot
    # and bin the promise if that failed — but the lane is not needed until the
    # command finishes, potentially minutes later, and a session that happens to
    # be mid-re-host at boot resolves fine a moment afterwards. Binning it there
    # threw away exactly the resume this mechanism exists to save.
    monkeypatch.setattr(session_target, "_proc_start", lambda pid: "999")
    monkeypatch.setattr(session_target, "resolve", lambda pid, **kw: (
        _ for _ in ()).throw(session_target.ResolveError("mid re-host")))
    armed = []
    monkeypatch.setattr(inject, "resume_watch", lambda item, **kw: armed.append(item))
    pending.record(_promise())
    assert inject.replay_pending() == {"pending": 1, "resumed": 1}
    assert len(armed) == 1
    assert len(pending.load_all()) == 1


def test_replay_is_the_same_code_for_both_lanes(monkeypatch):
    # There is one `resume_watch`, and it takes no transport. A background
    # promise and a tmux one go through it identically.
    monkeypatch.setattr(session_target, "_proc_start", lambda pid: "999")
    armed = []
    monkeypatch.setattr(inject, "resume_watch", lambda item, **kw: armed.append(item))
    pending.record(_promise(repl_pid=1, hosting="tmux"))
    pending.record(_promise(repl_pid=2, hosting="background"))
    inject.replay_pending()
    assert sorted(i.hosting for i in armed) == ["background", "tmux"]
