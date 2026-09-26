"""The send transaction: detect → guard → write → commit → confirm, three times.

A send used to be a blind write — paste, Enter, hope, report success. Then it
became a write gated on reading the text back off the lane, and that gate is what
these tests mostly exist to say is gone.

The gate was wrong twice over. It could not work on the daemon lane, which hands
back a stream of the TUI's repaint deltas rather than a rendered screen, so "I
cannot see it" was read as "it never arrived" and Enter was withheld from
sessions that had taken the paste perfectly well. And it could not work on tmux
either, though that took a year longer to surface: Claude Code stops painting its
composer while it compacts, so the one window every deferred resume is aimed at
is the one window the screen lies about.

So there is no lane flag here any more, and no fake that renders. Every lane is
written to and committed to, and the answer to "did it land" comes from the
session's own record and transcript — the same two signals on every transport,
which is what makes one code path defensible. What a lane may still say is that
it *refused* the write; that is :meth:`check_not_rejected`, a negative check
only, and the fakes model it with ``fail_on=("check",)``.
"""
from __future__ import annotations

import logging
from contextlib import contextmanager

import pytest

pytestmark = [pytest.mark.smoke]

from awm.reflection import inject, session_target, tmux_inject, watcher


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
    """A prompt box, with no opinion about what is on anybody's screen.

    ``deaf`` is a session that takes the keystrokes and starts no turn — the one
    thing past Enter the record can prove, and the only reason a retry is allowed
    to cross the commit. ``fail_on`` names the verbs that blow up; ``"check"`` is
    the daemon lane's ``auth-required``, the sole way a lane can report that what
    it was just handed was discarded.
    """

    def __init__(self, events, *, fail_on=None, record=None, deaf=False):
        self.events = events
        self._fail_on = fail_on or ()
        self._record = record
        self._deaf = deaf

    label = "the fake lane"

    def _maybe_fail(self, verb):
        if verb in self._fail_on:
            raise tmux_inject.TmuxError(f"{verb} blew up")

    def clear(self):
        self.events.append("clear")
        self._maybe_fail("clear")

    def write(self, text):
        self.events.append(f"write:{text}")
        self._maybe_fail("write")

    def check_not_rejected(self):
        self.events.append("check")
        self._maybe_fail("check")

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
    """A lane shaped like the real background one. Shaped like the tmux one too.

    Kept as a name rather than inlined because several tests below read better
    for saying which lane they are standing in — but there is deliberately
    nothing left to distinguish it, and that is the change.
    """
    return FakeWriter(events, record=record, **kw)


# ---------------------------------------------------------------------------
# The happy path
# ---------------------------------------------------------------------------

def test_a_clean_send_writes_checks_then_commits(monkeypatch):
    # Three verbs, in this order, and no fourth. Nothing is read off the lane
    # before Enter, because nothing a lane could say there is worth a veto.
    events = []
    rec = FakeRecord()
    w = FakeWriter(events, record=rec)
    _, result = deliver([w], monkeypatch, record=rec)
    assert result.submitted is True
    assert result.lane is LANE
    assert result.confirmed == inject.CONFIRMED_RECORD
    assert events == ["write:/compact", "check", "commit"]


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
# The screen is not consulted — on any lane
# ---------------------------------------------------------------------------

def test_the_transaction_never_asks_a_lane_what_is_on_its_screen(monkeypatch):
    # A writer that has no read-back at all must drive the whole transaction. If
    # anything in the sender ever reaches for one again, this raises rather than
    # quietly re-growing a per-lane flag to decide whether to believe it.
    class NoScreen(FakeWriter):
        def __getattr__(self, name):
            raise AssertionError(f"the sender asked the lane for {name!r}")

    events = []
    rec = FakeRecord()
    _, result = deliver([NoScreen(events, record=rec)], monkeypatch, record=rec)
    assert result.submitted is True
    assert events == ["write:/compact", "check", "commit"]


def test_a_lane_that_never_echoes_still_submits(monkeypatch):
    # The original regression, from the daemon side: a background session's PTY
    # hands back the TUI's repaint deltas and often repaints nothing for a paste,
    # though every byte arrived. Withholding Enter here is what stopped
    # background sessions compacting themselves.
    events = []
    rec = FakeRecord()
    _, result = deliver([daemon(events, rec)], monkeypatch, record=rec)
    assert result.submitted is True
    assert result.confirmed == inject.CONFIRMED_RECORD
    assert events.count("commit") == 1
    assert events.count("write:/compact") == 1, "and without needing a retry"


def test_a_resume_into_a_compacting_session_commits_once_and_is_enqueued(
        monkeypatch):
    # THE regression this change exists for. 2026-09-07: a session's own
    # `/compact` ran for 94 seconds, and Claude Code does not paint its composer
    # while it compacts — so the pane read back empty, the sender called the
    # paste missing and withheld Enter, twelve times. The session came back on a
    # fresh context with nothing to do.
    #
    # A compacting session is `busy` and its transcript carries the enqueue. That
    # pair is the whole answer, and it is the same pair on every lane.
    class Tail:
        def poll(self): return True
        def watch(self, _t): pass
        def landed(self, _t): return True

    events = []
    rec = FakeRecord(status="busy")
    _, result = deliver([FakeWriter(events, record=rec, deaf=True)], monkeypatch,
                        record=rec, tail=Tail())
    assert result.submitted is True
    assert result.confirmed == inject.CONFIRMED_ENQUEUED
    assert events == ["write:/compact", "check", "commit"], \
        "written once, submitted once — no attempt refused itself"


# ---------------------------------------------------------------------------
# What a lane may still say: that it refused the write
# ---------------------------------------------------------------------------

def test_a_lane_that_reports_the_write_discarded_never_commits(monkeypatch):
    # The daemon lane answers an unauthenticated raw frame with an
    # `auth-required` control frame and drops the input. That used to surface as
    # a side effect of reading the screen for verification; deleting the read
    # would have deleted the check with it, so it is its own verb now.
    events = []
    rec = FakeRecord()
    with pytest.raises(inject.DeliveryError):
        deliver([FakeWriter(events, fail_on=("check",), record=rec)],
                monkeypatch, record=rec)
    assert "commit" not in events, "a refused write must not be submitted"
    assert events.count("write:/compact") == 3, "and it is retried"


def test_the_rejection_check_sits_between_the_write_and_the_commit(monkeypatch):
    # Order matters: before the write there is nothing to have been refused, and
    # after Enter it is too late to withhold it.
    events = []
    rec = FakeRecord()
    deliver([FakeWriter(events, record=rec)], monkeypatch, record=rec)
    assert events.index("write:/compact") < events.index("check") < \
        events.index("commit")


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


def test_a_session_held_at_shell_by_a_background_task_is_confirmed_by_record(
        monkeypatch):
    # Live probe, 2026-08-17: a session with a `sleep 600` running took its resume
    # straight in as an ordinary user message, and was reported `enqueued`
    # because `shell` was read as mid-turn. Confirmation asks the same question
    # the watcher does, or the two drift apart on exactly the case they exist for.
    class Tail:
        def poll(self): return True
        def watch(self, _t): pass
        def landed(self, _t): return True
        def tool_call_in_flight(self): return False
        def queued(self, _t): return False

    monkeypatch.setattr(watcher, "now_ms",
                        lambda: 1000 + int(watcher.SHELL_SETTLE_S * 1000) + 1)
    events = []
    rec = FakeRecord(status="shell")
    _, result = deliver([daemon(events, rec)], monkeypatch, record=rec, tail=Tail())
    assert result.confirmed == inject.CONFIRMED_RECORD


def test_a_busy_session_whose_transcript_shows_the_line_is_evidence(monkeypatch):
    # `queued` was always an inference: nothing checked it, it was returned
    # purely because the sampled status was non-settled. The session's transcript
    # records the enqueue, which turns the commonest result of all into an
    # observation — and keeps the word `queued` for the case with nothing to read.
    class Tail:
        def poll(self): return True
        def watch(self, _t): pass
        def landed(self, _t): return True

    events = []
    rec = FakeRecord(status="busy")
    _, result = deliver([daemon(events, rec, deaf=True)], monkeypatch, record=rec,
                        tail=Tail())
    assert result.confirmed == inject.CONFIRMED_ENQUEUED


def test_a_session_behind_a_modal_is_refused_before_anything_is_typed(
        monkeypatch):
    # `waiting` is a blocking dialog, not a turn in flight: a paste goes into the
    # dialog rather than into a queue. The old screen gate caught this by
    # accident — the composer is not painted behind a modal — but only *after*
    # leaving a paste in it. Asking the record instead refuses with the box
    # untouched, which is strictly the better failure.
    events = []
    rec = FakeRecord(status="waiting")
    with pytest.raises(inject.DeliveryError) as err:
        deliver([daemon(events, rec)], monkeypatch, record=rec)
    assert events == [], "nothing typed, nothing cleared, nothing submitted"
    assert "waiting for an answer" in str(err.value)


def test_a_give_up_against_a_modal_does_not_clear_its_prompt(monkeypatch):
    # The give-up wipe is only safe because by then the one thing in the box is
    # ours. Against a modal nothing of ours ever went in, and a Ctrl-U aimed at
    # the prompt lands in the dialog instead — answering somebody's question for
    # them, unasked.
    events = []
    rec = FakeRecord(status="waiting")
    with pytest.raises(inject.DeliveryError):
        deliver([daemon(events, rec)], monkeypatch, record=rec)
    assert "clear" not in events


def test_a_modal_that_opens_after_the_sample_is_still_named_apart_from_queued():
    # The guard above reads the record before the write; a session can open a
    # dialog in the gap. Confirmation still has a word for it, and it is not
    # `queued` — the two want opposite things from whoever reads the result.
    assert inject._confirm_submit(4242, ("waiting", 1000)) == \
        inject.CONFIRMED_BLOCKED


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
    # One code path for every transport. There were two hand-written watchers
    # once and they drifted; the confirming half must not go the same way — and
    # since the lanes no longer differ in anything the sender can see, the only
    # way they could drift again is if something reintroduced a branch.
    for make in (FakeWriter, daemon):
        events = []
        rec = FakeRecord()
        _, result = deliver([make(events, rec) if make is daemon
                             else make(events, record=rec)],
                            monkeypatch, record=rec)
        assert result.confirmed == inject.CONFIRMED_RECORD


# ---------------------------------------------------------------------------
# Retrying
# ---------------------------------------------------------------------------

def test_three_attempts_then_a_failure_naming_all_three(monkeypatch, caplog):
    events = []
    rec = FakeRecord()
    w = FakeWriter(events, fail_on=("write",), record=rec)
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
        deliver([FakeWriter(events, fail_on=("write",), record=rec)],
                monkeypatch, record=rec)
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
        deliver([FakeWriter(events, fail_on=("write",), record=rec)],
                monkeypatch, record=rec)
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
        deliver([FakeWriter([], fail_on=("write",))], monkeypatch,
                detects=detects, record=FakeRecord())
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
