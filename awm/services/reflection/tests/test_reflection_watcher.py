"""The one watcher, which both lanes share.

These are deliberately transport-free: the watcher reads the session's own record
and nothing else, so nothing here fakes a pane or a socket. That is the property
under test as much as any individual case — there used to be two of these, one
per backend, and they drifted until only the daemon one worked.
"""
from __future__ import annotations

import pytest

pytestmark = [pytest.mark.smoke]

from awm.reflection import watcher


class Clock:
    """Monotonic-ish clock advancing ``step`` seconds per call."""

    def __init__(self, step: float = 0.0):
        self.t = 0.0
        self.step = step

    def __call__(self) -> float:
        v = self.t
        self.t += self.step
        return v


class Tail:
    """A transcript that answers whatever the test says it answers.

    The default is the tri-state "don't know" a missing transcript gives, which
    is what keeps a `shell` unsettled — so a test that says nothing about the
    transcript is testing the record alone, as these all used to.
    """

    def __init__(self, in_flight=None, queued=None, consumed=None,
                 started=None, compacted=None, started_at=None):
        self._in_flight = in_flight
        self._queued = queued
        self._consumed = consumed
        self._started = started
        self._compacted = compacted
        # When set, the start line is dated: the watcher must reject one stamped
        # before it typed, because an earlier compaction left an identical line.
        self._started_at = started_at
        self.polls = 0

    def poll(self):
        self.polls += 1
        return self._in_flight is not None

    def watch(self, _text):
        pass

    def tool_call_in_flight(self):
        return self._in_flight

    def queued(self, _text):
        return self._queued

    def consumed(self, _text):
        return self._consumed

    def started(self, _text, *, since_ms=None):
        if self._started_at is not None:
            return self._started_at > (since_ms or 0)
        return self._started

    def compacted(self, *, since_ms=None):
        return self._compacted


def _run(read, injected_at_ms=1000, alive=True, step=0.0, tail=None, text=""):
    """Drive ``await_completion`` with ``read`` standing in for the record."""
    orig = watcher.read_status
    watcher.read_status = read
    try:
        return watcher.await_completion(
            4242, injected_at_ms=injected_at_ms, text=text,
            sleep=lambda _s: None, tail=tail if tail is not None else Tail(),
            clock=Clock(step), proc_start=lambda _p: "123" if alive else None)
    finally:
        watcher.read_status = orig


def run(samples, *, injected_at_ms=1000, alive=True, step=0.0, tail=None,
        text=""):
    """Drive the watcher over a scripted list of ``(status, statusUpdatedAt)``.

    The last sample repeats forever, so a script that never settles runs to the
    hard cap — which is what a non-zero ``step`` makes reachable.
    """
    served = list(samples)

    def read(_pid):
        return served.pop(0) if len(served) > 1 else served[0]

    return _run(read, injected_at_ms, alive, step, tail, text)


# ---------------------------------------------------------------------------
# The signal that matters: the command has begun running
# ---------------------------------------------------------------------------

def test_a_command_seen_to_start_fires_at_once_however_busy_the_session_is():
    # The change. A compacting session is `busy` from the driving turn straight
    # through compaction, and everything typed in that window is drained into the
    # turn that begins when it ends — so this is the moment to deliver, not
    # something to wait out.
    assert run([("busy", 2000)], text="/compact", step=100.0,
               tail=Tail(started=True)) == watcher.STARTED_OUTCOME


def test_a_start_line_from_an_earlier_compaction_does_not_fire():
    # The session compacted half an hour ago, so its transcript already holds the
    # identical `/compact` start line. Firing on it would put the resume in the
    # queue *ahead* of the command it is meant to follow.
    assert run([("busy", 2000)], injected_at_ms=1_000, text="/compact",
               step=100.0,
               tail=Tail(started_at=500)) == watcher.TIMED_OUT


def test_a_finished_compaction_fires_even_if_the_start_line_was_missed():
    assert run([("busy", 2000)], text="/compact", step=100.0,
               tail=Tail(compacted=True)) == watcher.STARTED_OUTCOME


def test_a_boundary_says_nothing_about_a_command_that_is_not_a_compaction():
    # Somebody else's compaction, or the session's own, is not evidence that the
    # `/model` we typed has run.
    assert run([("busy", 2000)], text="/model opus", step=100.0,
               tail=Tail(compacted=True)) == watcher.TIMED_OUT


def test_a_started_command_still_waits_for_a_dialog_to_clear():
    # `waiting` means a modal owns the keyboard, so a paste is swallowed rather
    # than queued. That outranks every other signal.
    assert run([("waiting", 2000)], text="/compact", step=100.0,
               tail=Tail(started=True)) == watcher.TIMED_OUT


def test_an_unreadable_transcript_falls_back_to_the_settled_path():
    # No start line to see, so the old two-condition rule decides — which is why
    # it is kept.
    assert run([("idle", 2000)], text="/compact") == watcher.SETTLED_OUTCOME


# ---------------------------------------------------------------------------
# The bug: a command that finished before the first sample
# ---------------------------------------------------------------------------

def test_a_command_already_finished_still_settles():
    # THE regression. The tmux watcher would not fire until it had personally
    # caught the session mid-turn, and a one-second turn is invisible to a
    # two-second poll — so a fast /compact stalled its resume for the full cap.
    # Here the very first sample is already idle-and-after-injection.
    assert run([("idle", 2000)]) == watcher.SETTLED_OUTCOME


def test_a_replayed_promise_settles_against_its_own_injection_time():
    # After a service restart the command ran *before* this process existed, so
    # asking "has it reacted since boot" would never be true. The question is
    # asked against the moment the command was injected, which `pending` carries
    # across the restart.
    assert run([("idle", 5_000)], injected_at_ms=1_000) == watcher.SETTLED_OUTCOME


def test_an_idle_from_before_the_injection_is_not_a_reaction():
    # The session is sitting idle, but that idleness predates our command — it
    # has not picked the command up yet. Firing here resumes on the old context.
    assert run([("idle", 500)], injected_at_ms=1_000, step=100.0) == watcher.TIMED_OUT


# ---------------------------------------------------------------------------
# Settling
# ---------------------------------------------------------------------------

def test_a_full_turn_then_idle_settles():
    assert run([("busy", 1100), ("busy", 1200), ("idle", 1300),
                ("idle", 1300), ("idle", 1300)]) == watcher.SETTLED_OUTCOME


def test_one_idle_sample_is_not_enough():
    # A single settled sample between two busy ones is a flap, not completion.
    assert run([("busy", 1100), ("idle", 1200), ("busy", 1300)],
               step=100.0) == watcher.TIMED_OUT


@pytest.mark.parametrize("status", ["busy", "waiting"])
def test_a_busy_or_blocked_session_never_settles(status):
    # `waiting` is a *blocking dialog* — a permission prompt or sandbox request —
    # and resuming into one pastes the prompt behind a modal that swallows it.
    # The daemon watcher used to treat `waiting` as settled and did exactly that.
    # Neither of these earns a second look from the transcript.
    assert run([(status, 2000)], step=100.0,
               tail=Tail(in_flight=False)) == watcher.TIMED_OUT


def test_a_shell_with_no_transcript_to_check_stays_unsettled():
    # `shell` is only ever settled on corroboration. With nothing readable there
    # is none, so it behaves exactly as it did before the transcript existed.
    assert run([("shell", 2000)], step=100.0) == watcher.TIMED_OUT


# ---------------------------------------------------------------------------
# The bug: a session that finished its turn but still holds a background task
# ---------------------------------------------------------------------------

@pytest.fixture
def frozen_clock(monkeypatch):
    """Wall clock far enough past the samples that a `shell` counts as stale."""
    monkeypatch.setattr(watcher, "now_ms",
                        lambda: 2000 + int(watcher.SHELL_SETTLE_S * 1000))


def test_a_stale_shell_with_only_background_work_settles(frozen_clock):
    # THE regression, and the reason every autopilot resume waited out the cap.
    # A Monitor or a backgrounded Bash holds the session at `shell` after its
    # turn has ended — for as long as that task runs, which can be hours. Its
    # transcript tail is clean, so the foreground is free and the resume can go.
    assert run([("shell", 2000)], text="/compact",
               tail=Tail(in_flight=False, queued=False)) == watcher.SETTLED_OUTCOME


def test_a_stale_shell_with_a_foreground_tool_call_does_not_settle(frozen_clock):
    # The other half of the same status. An unanswered tool_use at the tail means
    # a turn is being driven right now, and a resume typed in would be queued.
    assert run([("shell", 2000)], step=100.0, text="/compact",
               tail=Tail(in_flight=True, queued=False)) == watcher.TIMED_OUT


def test_a_shell_that_only_just_arrived_is_not_yet_evidence(monkeypatch):
    # Statuses flap between `busy` and `shell` every few seconds while a turn is
    # driven. Only a `shell` that has stopped moving says anything.
    monkeypatch.setattr(watcher, "now_ms", lambda: 2000 + 1_000)
    assert run([("shell", 2000)], step=100.0, text="/compact",
               tail=Tail(in_flight=False, queued=False)) == watcher.TIMED_OUT


def test_our_own_command_still_queued_holds_the_resume_back(frozen_clock):
    # The session took the keystrokes but has not run the command yet — it is
    # sitting in the queue behind whatever else is there. Resuming now co-queues
    # the resume with the /compact it is supposed to follow.
    assert run([("shell", 2000)], step=100.0, text="/compact",
               tail=Tail(in_flight=False, queued=True)) == watcher.TIMED_OUT


def test_seeing_the_command_consumed_is_a_reaction(frozen_clock):
    # The record's timestamp is the fallback signal for "it took our command".
    # The transcript says so directly, which is what lets a session whose record
    # has not been rewritten still be seen to have reacted.
    assert run([("idle", 500)], injected_at_ms=1_000, text="/compact",
               tail=Tail(in_flight=False, queued=False,
                         consumed=True)) == watcher.SETTLED_OUTCOME


def test_an_unrecognised_status_keeps_us_waiting():
    # The vocabulary is closed today, but a CLI update could grow it. An unknown
    # value must read as "not settled" so a new state cannot fire the resume
    # early — the failure direction that costs nothing.
    assert run([("thinking-very-hard", 2000)], step=100.0) == watcher.TIMED_OUT


# ---------------------------------------------------------------------------
# Robustness while the session is being moved around
# ---------------------------------------------------------------------------

def test_an_unreadable_record_on_a_live_process_is_not_death():
    # A session being attached or backgrounded rewrites this file, and a read
    # landing inside that rewrite comes back empty. Calling that "vanished" would
    # drop the resume for a session that is merely being moved — which is the
    # attached → bg → foreground shuffle this has to ride through.
    served = [None, None, ("idle", 2000), ("idle", 2000), ("idle", 2000)]

    def read(_pid):
        return served.pop(0) if len(served) > 1 else served[0]

    assert _run(read, 1000, True, 0.0) == watcher.SETTLED_OUTCOME


def test_an_unreadable_record_on_a_dead_process_is_death():
    assert run([None], alive=False) == watcher.VANISHED


def test_the_hard_cap_reports_itself_rather_than_hanging(caplog):
    # Reaching the cap is a refusal to deliver, and it says so — at WARNING now
    # rather than ERROR, because the round ending is no longer the end of the
    # promise: the session is asked to pause and waited on again.
    import logging
    with caplog.at_level(logging.WARNING, logger="awm.reflection.watcher"):
        assert run([("busy", 2000)], step=100.0) == watcher.TIMED_OUT
    assert any("still owed its resume" in r.getMessage()
               and r.levelno >= logging.WARNING for r in caplog.records)


def test_the_watcher_imports_no_transport():
    # The separation is the point. A watcher that reaches for a pane or a socket
    # is a watcher that can drift from the other lane again.
    import inspect

    from awm.reflection import watcher as w
    src = inspect.getsource(w)
    assert "tmux_inject" not in src
    assert "daemon_inject" not in src
