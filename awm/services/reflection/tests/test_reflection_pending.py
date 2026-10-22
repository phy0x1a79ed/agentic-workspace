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
from dataclasses import replace
from contextlib import contextmanager

import pytest

pytestmark = [pytest.mark.smoke]

from awm.reflection import inject, pending, session_target, watcher


LANE = session_target.TmuxLane(pane="%7", session_id="sid-1", repl_pid=4242,
                               name="test")


class Tail:
    """A transcript that answers whatever the test says it answers."""

    def __init__(self, in_flight=None, landed=None, started=False,
                 landed_since=lambda: False):
        self._in_flight = in_flight
        self._landed = landed
        self._started = started
        self._landed_since = landed_since

    def poll(self):
        return self._in_flight is not None

    def watch(self, _text):
        pass

    def expect(self, _text):
        pass

    def tool_call_in_flight(self):
        return self._in_flight

    def landed(self, _text, *, since_ms=None):
        if since_ms is not None:
            return self._landed_since()
        return self._landed

    def consumed(self, _text):
        return self._landed

    def started(self, _text, *, since_ms=None):
        return self._started

    def compacted(self, *, since_ms=None):
        return False

    def queued(self, _text):
        return False


class Stop(Exception):
    """Breaks out of a wait that is now deliberately unbounded."""


def _outcomes(*sequence):
    """An ``await_completion`` that returns each outcome in turn, then stops.

    The holding loop no longer gives up on a session that has not reached its
    command, so a test that wants to see several rounds has to end the loop
    itself. That is the behaviour under test, not a testing inconvenience.
    """
    remaining = list(sequence)

    def fake(*_a, **_kw):
        if not remaining:
            raise Stop
        return remaining.pop(0)
    return fake


PROMISED_AT_MS = int(time.time() * 1000)


def _promise(**over) -> pending.Pending:
    """A promise stamped at one fixed moment, for a session that is alive.

    The stamp is what tells a watcher its promise is still the newest, so two
    calls must agree on it.
    """
    base = dict(repl_pid=4242, proc_start="999", session_id="sid-1",
                text="/compact", followup="resume",
                injected_at_ms=PROMISED_AT_MS, name="test",
                hosting="tmux")
    base.update(over)
    return pending.Pending(**base)


class Writer:
    """A lane that accepts anything and refuses nothing."""

    label = "the fake lane"

    def __init__(self, seen):
        self.seen = seen

    def clear(self):
        pass

    def write(self, text):
        self.seen.append(text)

    def check_not_rejected(self):
        pass

    def commit(self):
        pass


@pytest.fixture(autouse=True)
def _no_round_backoff(monkeypatch):
    """Collapse the between-rounds hold, except where a test is about it.

    The real values are tens of seconds, deliberately — they are sized against a
    94-second compaction, not against a test suite. A test that wants to see the
    spacing sets them back.
    """
    monkeypatch.setattr(inject, "ROUND_BACKOFF_S", (0.0,))


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


def test_a_started_command_is_resumed_without_waiting_for_a_free_session(
        lane, monkeypatch):
    # The change this whole round is about. A session that has begun running its
    # `/compact` is by definition mid-turn and its status is `busy` — and that is
    # precisely when the resume must go in, because everything typed during a
    # compaction is drained into the turn that starts when it ends. Requiring the
    # session to look free first is what cost 27 seconds in the good case and an
    # hour in the bad one.
    monkeypatch.setattr(watcher, "await_completion", _outcomes(
        watcher.STARTED_OUTCOME))
    monkeypatch.setattr(watcher, "read_status", lambda pid: ("busy", 9))
    pending.record(_promise())
    inject._await_and_resume(_promise(), tail=Tail(in_flight=False, landed=True))
    assert lane == ["resume"]
    assert pending.load_all() == []


def test_a_session_that_never_reaches_its_command_is_never_given_up_on(
        lane, monkeypatch, caplog):
    # A promise used to be abandoned after four rounds. One session's driving turn
    # ran 63 minutes: its promise was binned three minutes before the `/compact`
    # began, and it then sat idle holding a resume nobody was waiting to give it.
    # Waiting is now bounded only by the session's own life.
    monkeypatch.setattr(watcher, "await_completion",
                        _outcomes(*[watcher.TIMED_OUT] * 8))
    pending.record(_promise())
    with caplog.at_level(logging.ERROR, logger="awm.reflection.inject"):
        with pytest.raises(Stop):
            inject._await_and_resume(_promise())
    assert "resume" not in lane, "a resume must not be typed in ahead of the command"
    [item] = pending.load_all()
    assert item.holds == 8
    assert not any("gave up" in r.getMessage() for r in caplog.records)


def test_a_session_holding_up_its_own_command_is_asked_to_pause(lane, monkeypatch):
    # Holding in silence is how an hour goes by: the session cannot see that it is
    # sitting on its own compaction. The pause request goes in as plain text —
    # which the queue hands to the *running* turn, unlike the slash command it is
    # about — and asks for a turn boundary and nothing else.
    monkeypatch.setattr(watcher, "await_completion", _outcomes(
        watcher.TIMED_OUT, watcher.STARTED_OUTCOME))
    monkeypatch.setattr(watcher, "read_status", lambda pid: ("busy", 9))
    pending.record(_promise())
    inject._await_and_resume(_promise(), tail=Tail(in_flight=False, landed=True))
    nudge, resumed = lane
    assert resumed == "resume"
    assert nudge.startswith("[awm reflection]")
    assert "/compact" in nudge and "end your turn" in nudge
    assert "not a new instruction" in nudge
    assert "\n" not in nudge, "a newline in a paste submits it early"


def test_a_round_that_expires_as_the_command_starts_resumes_instead_of_nudging(
        lane, monkeypatch):
    # Measured in the first live drill: the round ended 377ms before the
    # `/compact` start line was written. The pause request that followed was
    # queued *during* compaction and would have been drained into the fresh
    # context alongside the resume — the one place it does harm.
    monkeypatch.setattr(watcher, "await_completion", _outcomes(watcher.TIMED_OUT))
    monkeypatch.setattr(watcher, "read_status", lambda pid: ("busy", 9))
    pending.record(_promise())
    inject._await_and_resume(
        _promise(), tail=Tail(in_flight=False, landed=True, started=True))
    assert lane == ["resume"], "no pause request once the command is running"
    assert pending.load_all() == []


def test_the_pause_request_is_rationed_and_the_wait_goes_quiet(lane, monkeypatch):
    # Text arriving unbidden in somebody's session is rationed. After the cap the
    # promise is still held — quietly, and for as long as the session lives.
    monkeypatch.setattr(watcher, "await_completion",
                        _outcomes(*[watcher.TIMED_OUT] * (inject.NUDGE_LIMIT + 2)))
    pending.record(_promise())
    with pytest.raises(Stop):
        inject._await_and_resume(_promise())
    assert len(lane) == inject.NUDGE_LIMIT
    [item] = pending.load_all()
    assert item.nudges == inject.NUDGE_LIMIT
    assert "holding quietly" in item.last_outcome


def test_a_busy_session_at_the_last_instant_holds_rather_than_types(
        lane, monkeypatch):
    # The watcher said settled, but a turn started in the gap between the wait
    # and the write. Holding the promise is only worth anything if the delivery
    # itself checks too. (A *started* command is the opposite case and skips this
    # check — see above.)
    monkeypatch.setattr(watcher, "await_completion",
                        _outcomes(watcher.SETTLED_OUTCOME))
    monkeypatch.setattr(watcher, "read_status", lambda pid: ("busy", 9))
    pending.record(_promise())
    with pytest.raises(Stop):
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
    assert lane == ["resume"] * inject.MAX_DELIVERY_ROUNDS
    [item] = pending.load_all()
    assert "never showed it arriving" in item.last_outcome


def test_a_resume_that_turns_up_after_its_verify_gave_up_is_not_sent_again(
        lane, monkeypatch):
    # 2026-10-09, Claude Code 2.1.295: a resume pasted behind `/compact` showed
    # in the transcript only when compaction ended, 70s on — after the verify
    # had called it lost. Each later round typed another copy in.
    monkeypatch.setattr(watcher, "await_completion",
                        lambda *a, **kw: watcher.STARTED_OUTCOME)
    monkeypatch.setattr(inject, "VERIFY_WAIT_S", 0.0)
    pending.record(_promise())
    tail = Tail(in_flight=False, landed=False, landed_since=lambda: bool(lane))
    inject._await_and_resume(_promise(), tail=tail)
    assert lane == ["resume"]
    assert pending.load_all() == []


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


# ---------------------------------------------------------------------------
# Spacing the delivery rounds
# ---------------------------------------------------------------------------

def test_consecutive_delivery_rounds_are_separated_in_time(lane, monkeypatch):
    # 2026-09-07: all four delivery rounds for one resume were spent inside 7.7
    # seconds of a `/compact` that ran for 94, because a failed round re-entered
    # the wait and the wait saw the command's own start line still sitting in the
    # transcript and returned "started" straight back. Whatever is wrong with a
    # lane at the moment a round fails does not change in 40ms.
    slept = []
    monkeypatch.setattr(inject, "ROUND_BACKOFF_S", (30.0, 60.0, 120.0))
    monkeypatch.setattr(watcher, "await_completion",
                        lambda *a, **kw: watcher.SETTLED_OUTCOME)
    monkeypatch.setattr(inject, "VERIFY_WAIT_S", 0.0)
    pending.record(_promise())
    inject._await_and_resume(_promise(), tail=Tail(in_flight=False, landed=False),
                             sleep=slept.append)
    assert lane == ["resume"] * inject.MAX_DELIVERY_ROUNDS
    holds = [s for s in slept if s >= 30.0]
    assert holds == [30.0, 60.0, 120.0], \
        "one hold before each re-armed round, escalating"
    assert sum(holds) > 94, "and the budget outlasts the longest compaction seen"


def test_the_first_round_is_not_held_up(lane, monkeypatch):
    # The spacing is between *retries*. A resume that is deliverable the first
    # time must go in the instant the session reaches its command.
    slept = []
    monkeypatch.setattr(inject, "ROUND_BACKOFF_S", (30.0, 60.0, 120.0))
    monkeypatch.setattr(watcher, "await_completion",
                        lambda *a, **kw: watcher.SETTLED_OUTCOME)
    pending.record(_promise())
    inject._await_and_resume(_promise(), sleep=slept.append)
    assert lane == ["resume"]
    assert [s for s in slept if s >= 30.0] == []


def test_a_round_that_ended_in_a_hold_re_arms_without_a_pause(lane, monkeypatch):
    # A session that has not reached its command yet is the mechanism working,
    # and waiting on it is unbounded by design. Only a failed *delivery* is
    # spaced; holding must stay as responsive as it was.
    slept = []
    monkeypatch.setattr(inject, "ROUND_BACKOFF_S", (30.0, 60.0, 120.0))
    monkeypatch.setattr(watcher, "await_completion",
                        _outcomes(*[watcher.TIMED_OUT] * 3))
    monkeypatch.setattr(watcher, "has_started", lambda *a, **kw: False)
    pending.record(_promise())
    with pytest.raises(Stop):
        inject._await_and_resume(_promise(), tail=Tail(in_flight=False),
                                 sleep=slept.append)
    assert [s for s in slept if s >= 30.0] == []


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


def test_pause_requests_are_counted_on_the_promise_so_a_restart_cannot_reset_them():
    # The cap has to survive a respawn: a session already asked three times must
    # not be asked three more by the process that replaces this one.
    item = pending.nudged(pending.nudged(_promise()))
    assert item.nudges == 2
    pending.record(item)
    [reloaded] = pending.load_all(proc_start=lambda pid: "999")
    assert reloaded.nudges == 2


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


# ---------------------------------------------------------------------------
# A resume that says what happened
# ---------------------------------------------------------------------------

CONTINUE = "Continue with what you were doing."
EFFORT = pending.Change("effort", "low", "k-effort")
RENAME = pending.Change("rename", "scout", "k-rename")


@pytest.mark.parametrize("changes,link,closing,expected", [
    ((EFFORT,), None, "",
     f"The effort change to low succeeded. {CONTINUE}"),
    ((pending.Change("model", "opus", "k"),), None, "",
     f"The model change to opus succeeded. {CONTINUE}"),
    ((RENAME,), None, "", f"The rename to scout succeeded. {CONTINUE}"),
    ((pending.Change("rc_on", "", "k"),), "https://claude.ai/code/session_01A", "",
     f"Remote Control is on: https://claude.ai/code/session_01A. {CONTINUE}"),
    ((pending.Change("rc_on", "", "k"),), None, "",
     f"Remote Control was turned on; run reflection link to read its link. {CONTINUE}"),
    ((pending.Change("effort", "low", "k", pending.FAILED, "the cursor was on No"),),
     None, "",
     f"The effort change to low did not apply (the cursor was on No). {CONTINUE}"),
    ((RENAME, EFFORT), None, "",
     f"The rename to scout and the effort change to low succeeded. {CONTINUE}"),
    ((RENAME, pending.Change("model", "opus", "k", pending.FAILED, "a hook asked")),
     None, "",
     f"The rename to scout succeeded. The model change to opus did not apply "
     f"(a hook asked). {CONTINUE}"),
    ((EFFORT,), None, "go on with T3",
     "The effort change to low succeeded. go on with T3"),
    ((), None, "", CONTINUE),
    ((), None, "go on with T3", "go on with T3"),
])
def test_the_resume_names_each_change_and_how_it_ended(changes, link, closing, expected):
    assert pending.render_followup(changes, link, closing) == expected


def test_a_settings_command_promises_a_resume_that_names_it(lane):
    inject.send("/effort low", caller_pid=4242, change=EFFORT, spawn=lambda fn: None)
    [item] = pending.load_all()
    assert item.followup == f"The effort change to low succeeded. {CONTINUE}"
    assert item.changes == (EFFORT,), "the change survives the replay's round trip"


def test_compact_and_message_self_keep_the_default_resume(lane):
    inject.send("/compact", caller_pid=4242, spawn=lambda fn: None)
    assert [i.followup for i in pending.load_all()] == [CONTINUE]


def _resume(item, monkeypatch):
    monkeypatch.setattr(watcher, "await_completion",
                        lambda *a, **kw: watcher.SETTLED_OUTCOME)
    inject._await_and_resume(item, tail=Tail(in_flight=False, landed=None))


def test_the_resume_is_worded_from_the_outcome_known_at_delivery(lane, monkeypatch):
    inject.send("/effort low", caller_pid=4242, change=EFFORT, spawn=lambda fn: None)
    [item] = pending.load_all()
    pending.settle(4242, EFFORT.key, pending.FAILED, "the cursor was on No")
    _resume(item, monkeypatch)
    assert lane[1:] == [f"The effort change to low did not apply (the cursor was "
                        f"on No). {CONTINUE}"]


def test_a_move_is_followed_by_an_mcp_reconnect_before_its_resume(lane, monkeypatch):
    move = pending.Change("cd", "/x", "k-cd")
    inject.send("/cd /x", caller_pid=4242, change=move, spawn=lambda fn: None)
    [item] = pending.load_all()
    _resume(item, monkeypatch)
    assert lane[1:] == [inject.RECONNECT_AFTER_MOVE,
                        f"The move to /x succeeded. {CONTINUE}"]


def test_a_resume_with_no_move_reconnects_nothing(lane, monkeypatch):
    inject.send("/effort low", caller_pid=4242, change=EFFORT, spawn=lambda fn: None)
    [item] = pending.load_all()
    _resume(item, monkeypatch)
    assert inject.RECONNECT_AFTER_MOVE not in lane


def test_an_applied_outcome_reads_as_success(lane, monkeypatch):
    inject.send("/effort low", caller_pid=4242, change=EFFORT, spawn=lambda fn: None)
    [item] = pending.load_all()
    pending.settle(4242, EFFORT.key, pending.APPLIED)
    _resume(item, monkeypatch)
    assert lane[1:] == [f"The effort change to low succeeded. {CONTINUE}"]


def test_a_replayed_promise_with_no_outcome_falls_back_to_the_plain_change_text(
        lane, monkeypatch):
    inject.send("/effort low", caller_pid=4242, change=EFFORT, spawn=lambda fn: None)
    [replayed] = pending.load_all()
    assert replayed.changes[0].outcome == ""
    _resume(replayed, monkeypatch)
    assert lane[1:] == [f"The effort change to low succeeded. {CONTINUE}"]


def test_an_outcome_survives_a_rewrite_by_a_watcher_that_has_not_seen_it(lane):
    inject.send("/effort low", caller_pid=4242, change=EFFORT, spawn=lambda fn: None)
    [stale] = pending.load_all()
    pending.settle(4242, EFFORT.key, pending.FAILED, "no")
    pending.record(pending.held(stale, "waiting"))
    [now] = pending.load_all()
    assert now.changes[0].outcome == pending.FAILED and now.holds == 1


def test_settling_a_change_nobody_carries_is_a_no_op(lane):
    assert pending.settle(4242, "nope", pending.APPLIED) is False


# ---------------------------------------------------------------------------
# One resume per session
# ---------------------------------------------------------------------------

def _two_self_commands():
    inject.send("/rename scout", caller_pid=4242, change=RENAME,
                spawn=lambda fn: None)
    inject.send("/effort low", caller_pid=4242, change=EFFORT,
                spawn=lambda fn: None)
    return pending.read(4242)


def _older_than(item, **over):
    """The first command's promise, as its own watcher still holds it."""
    return replace(item, injected_at_ms=item.injected_at_ms - 5, **over)


MERGED = (f"The rename to scout and the effort change to low succeeded. "
          f"{CONTINUE}")


def test_a_second_self_command_takes_over_the_promise_and_carries_the_first(lane):
    item = _two_self_commands()
    assert item.text == "/effort low"
    assert [c.key for c in item.changes] == [RENAME.key, EFFORT.key]
    assert item.followup == MERGED


def test_two_self_commands_send_exactly_one_resume(lane, monkeypatch):
    # The observed failure: both watchers delivered, and after the back-off both
    # delivered again, so the session got four resumes for two commands.
    newer = _two_self_commands()
    older = _older_than(newer, text="/rename scout", changes=(RENAME,))
    tail = Tail(in_flight=False, landed=None)
    monkeypatch.setattr(watcher, "await_completion",
                        lambda *a, **kw: watcher.SETTLED_OUTCOME)
    inject._await_and_resume(older, tail=tail)
    assert lane[2:] == [], "the older watcher stands down"
    assert pending.read(4242) is not None, "and leaves the newer promise on disk"
    inject._await_and_resume(newer, tail=tail)
    assert lane[2:] == [MERGED]


def test_an_older_watcher_that_wakes_after_the_newer_resume_went_in_stays_quiet(
        lane, monkeypatch):
    newer = _two_self_commands()
    tail = Tail(in_flight=False, landed=None)
    monkeypatch.setattr(watcher, "await_completion",
                        lambda *a, **kw: watcher.SETTLED_OUTCOME)
    inject._await_and_resume(newer, tail=tail)
    assert lane[2:] == [MERGED] and pending.read(4242) is None
    inject._await_and_resume(_older_than(newer, text="/rename scout"), tail=tail)
    assert lane[2:] == [MERGED], "no second resume once the first has gone in"


def test_a_custom_closing_prompt_is_kept_beside_a_later_settings_change(lane):
    inject.send("/compact", caller_pid=4242, followup="go on with T3",
                spawn=lambda fn: None)
    inject.send("/effort low", caller_pid=4242, change=EFFORT, spawn=lambda fn: None)
    assert pending.read(4242).followup == "The effort change to low succeeded. go on with T3"


# ---------------------------------------------------------------------------
# A session whose transcript moves under the wait
# ---------------------------------------------------------------------------

def test_a_verify_that_could_not_read_the_transcript_is_unknown_not_a_miss(
        monkeypatch):
    # A tail that read the file once and has lost it since still answers
    # `landed() is False` from what it read before. That is not evidence the
    # text failed to arrive, and a False here is what licenses a retype.
    monkeypatch.setattr(inject, "VERIFY_WAIT_S", 0.0)
    lost = Tail(in_flight=None, landed=False)
    assert inject._verify_landed(lost, "resume", sleep=lambda _s: None) is None


def test_a_session_that_moves_mid_wait_is_reconnected_and_resumed_once(
        tmp_path, monkeypatch):
    # 2026-10-19, avatar-3c: after a self-`/cd`, Claude Code moved the transcript
    # into the new directory's slug. The tail stayed bound to the old path, every
    # verify read nothing and called the resume lost, and each round typed the
    # reconnect and the resume in again.
    import json
    from awm.reflection import transcript

    projects = tmp_path / "projects"
    before, after = projects / "-home-tony-before", projects / "-home-tony-after"
    before.mkdir(parents=True)
    after.mkdir()
    records = tmp_path / "sessions"
    records.mkdir()
    record = records / "4242.json"
    record.write_text(json.dumps({"sessionId": "sid-1", "cwd": "/home/tony/before"}))
    monkeypatch.setattr(transcript, "PROJECTS_DIR", projects)
    monkeypatch.setattr(session_target, "SESSIONS_DIR", records)
    original = before / "sid-1.jsonl"
    original.write_text(json.dumps(
        {"type": "user", "message": {"role": "user", "content": "earlier work"}})
        + "\n")

    seen: list[str] = []

    class TranscribingWriter(Writer):
        """Each line it is given turns up in the transcript, wherever that is now."""

        def write(self, text):
            super().write(text)
            [live] = projects.glob("*/sid-1.jsonl")
            entry = {"type": "user", "timestamp": "2099-01-01T00:00:00.000Z",
                     "message": {"role": "user", "content": text}}
            with live.open("a", encoding="utf-8") as fp:
                fp.write(json.dumps(entry) + "\n")

    @contextmanager
    def open_lane(_lane, **_kw):
        yield TranscribingWriter(seen)

    monkeypatch.setattr(inject, "_open_lane", open_lane)
    monkeypatch.setattr(session_target, "resolve", lambda pid, **kw: LANE)
    monkeypatch.setattr(session_target, "_proc_start", lambda pid: "999")

    def wait_then_move(*_a, tail, **_kw):
        tail.poll()  # the watcher has been reading the old file all along
        if original.exists():
            record.write_text(json.dumps({"sessionId": "sid-1",
                                          "cwd": "/home/tony/after"}))
            original.rename(after / original.name)
        return watcher.SETTLED_OUTCOME

    monkeypatch.setattr(watcher, "await_completion", wait_then_move)
    monkeypatch.setattr(inject, "VERIFY_WAIT_S", 0.05)
    monkeypatch.setattr(inject, "VERIFY_POLL_S", 0.01)

    move = pending.Change("cd", "/home/tony/after", "k-cd")
    item = _promise(text="/cd /home/tony/after", changes=(move,))
    pending.record(item)
    inject._await_and_resume(item, tail=transcript.Tail(4242),
                             read_status=lambda _pid: None)

    assert seen.count(inject.RECONNECT_AFTER_MOVE) == 1
    resumes = [t for t in seen if t != inject.RECONNECT_AFTER_MOVE]
    assert resumes == [f"The move to /home/tony/after succeeded. {CONTINUE}"]
    assert pending.load_all() == []
