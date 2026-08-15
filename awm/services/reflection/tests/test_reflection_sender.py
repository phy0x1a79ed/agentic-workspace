"""The send transaction: detect → write → commit → confirm, up to three times.

A send used to be a blind write — paste, Enter, hope, report success. Then it
became a write gated on reading the text back off the lane, which broke every
background session: that lane returns a stream of the TUI's repaint deltas, not a
rendered screen, so "I cannot see it" was read as "it never arrived" and Enter
was withheld from sessions that had received the paste perfectly well.

So the fakes here come in two shapes, and the difference between them is the
whole point. A *rendering* lane (tmux, ``capture-pane``) shows what is on screen,
so a missing probe is real evidence. A *non-rendering* lane (the daemon PTY) may
show nothing at all, and the transaction has to submit anyway and get its answer
from the session's own status record instead. The old fake echoed what was
written to it by construction, which erased exactly this distinction — which is
why the suite was green while self-compaction was dead.
"""
from __future__ import annotations

import logging
from contextlib import contextmanager

import pytest

pytestmark = [pytest.mark.smoke]

from awm.reflection import inject, session_target, tmux_inject


LANE = session_target.TmuxLane(pane="%7", session_id="sid", repl_pid=4242,
                               name="test")


class FakeRecord:
    """The session's own ``~/.claude/sessions/<pid>.json``, as the sender reads it.

    Claude Code writes this on *transitions*, so ``at`` moves when the session
    starts doing something and not otherwise — which is what makes "it moved"
    mean "it took our line".
    """

    def __init__(self, status="idle", at=1000, *, readable=True):
        self.status = status
        self.at = at
        self.readable = readable
        self.reads = 0

    def __call__(self, _pid):
        self.reads += 1
        return (self.status, self.at) if self.readable else None

    def consumed(self) -> None:
        """The session started a turn on what we submitted."""
        self.status, self.at = "busy", self.at + 1


class FakeWriter:
    """A prompt box that may or may not admit to what was typed into it.

    ``shows`` is whether the read-back reflects the write; ``renders`` is whether
    a read-back that does not reflect it means anything. tmux is
    ``renders=True``; the daemon PTY is ``renders=False`` and routinely
    ``shows=False`` at the same time, which is not a fault.
    """

    def __init__(self, events, *, shows=True, renders=True, fail_on=None,
                 screen="", record=None, deaf=False):
        self.events = events
        self._shows = shows
        self.read_back_is_evidence = renders
        self._fail_on = fail_on or ()
        self._screen = screen
        self._record = record
        self._deaf = deaf

    label = "the fake lane"

    def _maybe_fail(self, verb):
        if verb in self._fail_on:
            raise tmux_inject.TmuxError(f"{verb} blew up")

    def read_back(self):
        self.events.append("read")
        return self._screen

    def clear(self):
        self.events.append("clear")
        self._maybe_fail("clear")
        self._screen = ""

    def write(self, text):
        self.events.append(f"write:{text}")
        self._maybe_fail("write")
        if self._shows:
            self._screen += text

    def commit(self):
        self.events.append("commit")
        self._maybe_fail("commit")
        if self._record is not None and not self._deaf:
            self._record.consumed()


def sender(*writers, events=None, detects=None):
    """Return ``(events, detect, open_lane)`` serving ``writers`` in order."""
    events = events if events is not None else []
    queue = list(writers)
    detected = detects if detects is not None else []

    def detect(pid):
        detected.append(pid)
        return LANE

    @contextmanager
    def open_lane(_lane, **_kw):
        yield queue.pop(0) if len(queue) > 1 else queue[0]

    return events, detect, open_lane


@pytest.fixture(autouse=True)
def _no_real_lanes(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("the transaction must go through the injected lane")
    monkeypatch.setattr(inject, "_open_lane", boom)


def deliver(writers, monkeypatch, *, detects=None, record=None, **kw):
    events, detect, open_lane = sender(*writers, detects=detects)
    monkeypatch.setattr(inject, "_open_lane", open_lane)
    kw.setdefault("sleep", lambda _s: None)
    if record is not None:
        kw.setdefault("read_status", record)
    return events, inject.deliver(4242, "/compact", detect=detect, **kw)


def daemon(events, record, **kw):
    """A lane shaped like the real background one: silent, and not evidence."""
    return FakeWriter(events, shows=False, renders=False, record=record, **kw)


# ---------------------------------------------------------------------------
# The happy path
# ---------------------------------------------------------------------------

def test_a_clean_send_reads_writes_reads_then_commits(monkeypatch):
    events = []
    rec = FakeRecord()
    w = FakeWriter(events, record=rec)
    _, result = deliver([w], monkeypatch, record=rec)
    assert result.submitted is True
    assert result.lane is LANE
    assert result.confirmed == inject.CONFIRMED_RECORD
    assert events == ["read", "write:/compact", "read", "commit"]


def test_the_first_attempt_never_clears_the_prompt(monkeypatch):
    # Clearing destroys whatever a human was mid-way through typing. A send that
    # works has no business doing that, so the wipe is a retry-only measure.
    events = []
    rec = FakeRecord()
    deliver([FakeWriter(events, record=rec)], monkeypatch, record=rec)
    assert "clear" not in events


def test_enter_false_writes_but_does_not_commit_or_claim_a_submit(monkeypatch):
    events = []
    _, result = deliver([FakeWriter(events)], monkeypatch, enter=False,
                        record=FakeRecord())
    assert result.submitted is False
    assert result.confirmed == inject.NOT_SUBMITTED
    assert "commit" not in events


# ---------------------------------------------------------------------------
# Lanes that do not render — the case this suite used to be blind to
# ---------------------------------------------------------------------------

def test_a_lane_that_never_echoes_still_submits(monkeypatch):
    # The regression. A background session's PTY hands back the TUI's repaint
    # deltas, and the TUI often repaints nothing for a paste. Every byte still
    # arrived. Withholding Enter here is what stopped background sessions from
    # compacting themselves, so the transaction must commit and let the session's
    # own record settle it.
    events = []
    rec = FakeRecord()
    _, result = deliver([daemon(events, rec)], monkeypatch, record=rec)
    assert result.submitted is True
    assert result.confirmed == inject.CONFIRMED_RECORD
    assert events.count("commit") == 1
    assert events.count("write:/compact") == 1, "and without needing a retry"


def test_text_that_never_appears_on_a_rendering_lane_is_still_refused(monkeypatch):
    # The other half: `capture-pane` renders current state, so a probe that is
    # not in the read-back is genuinely not on screen. Demoting the check to
    # advisory everywhere would have thrown away a real signal.
    events = []
    rec = FakeRecord()
    with pytest.raises(inject.DeliveryError):
        deliver([FakeWriter(events, shows=False, record=rec)], monkeypatch,
                record=rec)
    assert "commit" not in events, "nothing may be submitted unverified there"


def test_verification_counts_occurrences_rather_than_presence(monkeypatch):
    # A session that compacted an hour ago still has "/compact" on screen. A
    # presence check would call this verified without a byte having landed.
    events = []
    stale = FakeWriter(events, shows=False, screen="… ❯ /compact\n⎿ Compacted")
    with pytest.raises(inject.DeliveryError):
        deliver([stale], monkeypatch, record=FakeRecord())


def test_a_wrapped_paste_still_verifies():
    # The TUI wraps long input and paints escape sequences at the boundary, so
    # the comparison is made on text with escapes and whitespace removed.
    before = "❯ "
    after = "❯ Please run this ex\x1b[0m\nactly as written"
    assert inject._landed(before, after, inject._probe(
        "Please run this exactly as written"))


# ---------------------------------------------------------------------------
# Confirming the submit from the session's own record
# ---------------------------------------------------------------------------

def test_a_settled_session_that_never_moves_did_not_take_the_line(monkeypatch):
    # The positive negative: it was idle, it stayed idle, its timestamp never
    # moved — so it started no turn and consumed nothing. That is the one thing
    # past Enter that is safe to repeat, and the only reason a retry is allowed
    # to cross the commit at all.
    events = []
    rec = FakeRecord()
    with pytest.raises(inject.DeliveryError) as err:
        deliver([daemon(events, rec, deaf=True)], monkeypatch, record=rec)
    assert events.count("commit") == 3, "a proven non-submit is retried"
    assert "consumed nothing" in str(err.value)


def test_a_busy_session_is_reported_queued_rather_than_confirmed(monkeypatch):
    # A session compacting itself is mid-turn by definition, so Claude Code
    # queues the line behind the turn that asked for it and nothing transitions.
    # There is nothing to observe; saying so beats inventing either answer.
    events = []
    rec = FakeRecord(status="busy")
    _, result = deliver([daemon(events, rec, deaf=True)], monkeypatch, record=rec)
    assert result.submitted is True
    assert result.confirmed == inject.CONFIRMED_QUEUED
    assert events.count("commit") == 1, "and it is not retried"


def test_a_session_behind_a_modal_is_not_called_queued(monkeypatch):
    # `waiting` is a blocking dialog, not a turn in flight. The line was most
    # likely swallowed rather than queued, and the two want opposite things from
    # whoever reads the result — so they do not share a word.
    events = []
    rec = FakeRecord(status="waiting")
    _, result = deliver([daemon(events, rec, deaf=True)], monkeypatch, record=rec)
    assert result.confirmed == inject.CONFIRMED_BLOCKED


def test_an_unreadable_record_is_not_read_as_a_failure(monkeypatch):
    # The record is rewritten whenever a session is attached or backgrounded. A
    # read landing inside that is a transient miss, and the watcher rides through
    # the same gap rather than calling the session gone.
    events = []
    rec = FakeRecord(readable=False)
    _, result = deliver([daemon(events, rec)], monkeypatch, record=rec)
    assert result.submitted is True
    assert result.confirmed == inject.CONFIRMED_UNREADABLE
    assert events.count("commit") == 1


def test_confirmation_does_not_care_which_lane_it_is(monkeypatch):
    # One code path for both transports. There were two hand-written watchers
    # once and they drifted; the confirming half must not go the same way.
    for renders in (True, False):
        events = []
        rec = FakeRecord()
        _, result = deliver(
            [FakeWriter(events, shows=renders, renders=renders, record=rec)],
            monkeypatch, record=rec)
        assert result.confirmed == inject.CONFIRMED_RECORD


# ---------------------------------------------------------------------------
# Retrying
# ---------------------------------------------------------------------------

def test_three_attempts_then_a_failure_naming_all_three(monkeypatch, caplog):
    events = []
    rec = FakeRecord()
    w = FakeWriter(events, shows=False, record=rec)
    with caplog.at_level(logging.WARNING, logger="awm.reflection.inject"):
        with pytest.raises(inject.DeliveryError) as err:
            deliver([w], monkeypatch, record=rec)
    assert events.count("write:/compact") == 3
    for n in (1, 2, 3):
        assert f"attempt {n}" in str(err.value)
    assert sum("attempt" in r.getMessage() for r in caplog.records) >= 3


def test_a_retry_clears_the_prompt_first(monkeypatch):
    # Otherwise a half-landed paste from the failed attempt is concatenated onto
    # by the next one, and the session submits a mangled line.
    events = []
    rec = FakeRecord()
    with pytest.raises(inject.DeliveryError):
        deliver([FakeWriter(events, shows=False, record=rec)], monkeypatch,
                record=rec)
    assert events.index("clear") > events.index("write:/compact"), \
        "attempt 1 writes before any clear happens"
    assert events.count("clear") == 3, \
        "attempts 2 and 3 clear on the way in, and the give-up clears on the way out"


def test_a_give_up_leaves_nothing_in_the_prompt(monkeypatch):
    # The last attempt does not clear after itself, so a failure used to leave
    # exactly one unsubmitted paste sitting in the box for whatever typed next to
    # concatenate onto — or for a stray Enter to submit minutes later.
    events = []
    rec = FakeRecord()
    with pytest.raises(inject.DeliveryError):
        deliver([FakeWriter(events, shows=False, record=rec)], monkeypatch,
                record=rec)
    assert events[-1] == "clear"


def test_a_transient_failure_is_crossed_and_said_out_loud(monkeypatch, caplog):
    events = []
    rec = FakeRecord()
    broken = FakeWriter(events, fail_on=("write",), record=rec)
    healthy = FakeWriter(events, record=rec)
    with caplog.at_level(logging.WARNING, logger="awm.reflection.inject"):
        _, result = deliver([broken, healthy], monkeypatch, record=rec)
    assert result.submitted is True
    assert any("attempt 2" in r.getMessage() and "earlier attempts failed"
               in r.getMessage() for r in caplog.records), \
        "a send that only worked on a retry must be visible afterwards"


def test_each_attempt_detects_the_lane_again(monkeypatch):
    # Detection sits inside the loop, immediately before the write. A lane
    # decided once at the top and reused is a lane that can go stale between
    # deciding it and using it — which is exactly how a re-homed pty is missed.
    detects = []
    with pytest.raises(inject.DeliveryError):
        deliver([FakeWriter([], shows=False)], monkeypatch, detects=detects,
                record=FakeRecord())
    # Three attempts, plus the one the give-up clear makes to find the prompt.
    assert detects == [4242, 4242, 4242, 4242]


def test_a_lane_that_cannot_even_be_detected_is_retried(monkeypatch):
    calls = []

    def detect(pid):
        calls.append(pid)
        raise session_target.ResolveError("roster is mid-rewrite")

    monkeypatch.setattr(inject, "_open_lane",
                        lambda *a, **k: pytest.fail("must not open a lane"))
    with pytest.raises(inject.DeliveryError):
        inject.deliver(4242, "/compact", detect=detect, sleep=lambda _s: None)
    assert len(calls) == 4, "three attempts, and the give-up clear tries too"


# ---------------------------------------------------------------------------
# The commit boundary
# ---------------------------------------------------------------------------

def test_a_broken_commit_is_never_retried(monkeypatch, caplog):
    # The retry boundary moved past Enter, but only for the case the record
    # *proves* nothing was consumed. A commit that blew up mid-way proves the
    # opposite of that: it may already be running, and `send` carries arbitrary
    # text, so running it twice is worse than not at all.
    events = []
    w = FakeWriter(events, fail_on=("commit",))
    with caplog.at_level(logging.ERROR, logger="awm.reflection.inject"):
        with pytest.raises(inject.CommitFailed):
            deliver([w], monkeypatch, record=FakeRecord())
    assert events.count("commit") == 1, "a commit failure must not be retried"
    assert any("did not complete" in r.getMessage() for r in caplog.records)


def test_a_commit_failure_is_a_delivery_error_for_the_caller():
    # So the adapter's one seam catches it like any other failure rather than
    # letting it escape as an unhandled exception.
    assert issubclass(inject.CommitFailed, inject.DeliveryError)
