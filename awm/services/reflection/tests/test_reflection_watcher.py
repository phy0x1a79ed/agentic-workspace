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


def _run(read, injected_at_ms=1000, alive=True, step=0.0):
    """Drive ``await_completion`` with ``read`` standing in for the record."""
    orig = watcher.read_status
    watcher.read_status = read
    try:
        return watcher.await_completion(
            4242, injected_at_ms=injected_at_ms, sleep=lambda _s: None,
            clock=Clock(step), proc_start=lambda _p: "123" if alive else None)
    finally:
        watcher.read_status = orig


def run(samples, *, injected_at_ms=1000, alive=True, step=0.0):
    """Drive the watcher over a scripted list of ``(status, statusUpdatedAt)``.

    The last sample repeats forever, so a script that never settles runs to the
    hard cap — which is what a non-zero ``step`` makes reachable.
    """
    served = list(samples)

    def read(_pid):
        return served.pop(0) if len(served) > 1 else served[0]

    return _run(read, injected_at_ms, alive, step)


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


@pytest.mark.parametrize("status", ["busy", "shell", "waiting"])
def test_nothing_but_idle_counts_as_settled(status):
    # `shell` is a tool call in flight. `waiting` is a *blocking dialog* — a
    # permission prompt or sandbox request — and resuming into one pastes the
    # prompt behind a modal that swallows it. The daemon watcher used to treat
    # `waiting` as settled and would have done exactly that.
    assert run([(status, 2000)], step=100.0) == watcher.TIMED_OUT


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
    import logging
    with caplog.at_level(logging.WARNING, logger="awm.reflection.watcher"):
        assert run([("busy", 2000)], step=100.0) == watcher.TIMED_OUT
    assert any("did not settle" in r.getMessage() for r in caplog.records)


def test_the_watcher_imports_no_transport():
    # The separation is the point. A watcher that reaches for a pane or a socket
    # is a watcher that can drift from the other lane again.
    import inspect

    from awm.reflection import watcher as w
    src = inspect.getsource(w)
    assert "tmux_inject" not in src
    assert "daemon_inject" not in src
