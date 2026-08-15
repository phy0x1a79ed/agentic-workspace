"""The send transaction: detect → write → verify → commit, up to three times.

A send used to be a blind write — paste, Enter, hope, report success. These pin
the shape that replaced it. The fake writer here stands in for either lane on
purpose: the transaction must not care which one it is driving, and a test that
needed a pane or a socket to express any of this would be evidence that it does.
"""
from __future__ import annotations

import logging
from contextlib import contextmanager

import pytest

pytestmark = [pytest.mark.smoke]

from awm.reflection import inject, session_target, tmux_inject


LANE = session_target.TmuxLane(pane="%7", session_id="sid", repl_pid=4242,
                               name="test")


class FakeWriter:
    """A prompt box that remembers what was typed into it.

    ``shows`` false models the one failure verification exists to catch: the
    write is accepted by the transport and never appears — a modal swallowed it,
    or the session is not reading its pty.
    """

    def __init__(self, events, *, shows=True, fail_on=None, screen=""):
        self.events = events
        self._shows = shows
        self._fail_on = fail_on or ()
        self._screen = screen

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


def deliver(writers, monkeypatch, *, detects=None, **kw):
    events, detect, open_lane = sender(*writers, detects=detects)
    monkeypatch.setattr(inject, "_open_lane", open_lane)
    return events, inject.deliver(4242, "/compact", detect=detect, **kw)


# ---------------------------------------------------------------------------
# The happy path
# ---------------------------------------------------------------------------

def test_a_clean_send_reads_writes_reads_then_commits(monkeypatch):
    events = []
    w = FakeWriter(events)
    _, (submitted, lane) = deliver([w], monkeypatch)
    assert submitted is True
    assert lane is LANE
    assert events == ["read", "write:/compact", "read", "commit"]


def test_the_first_attempt_never_clears_the_prompt(monkeypatch):
    # Clearing destroys whatever a human was mid-way through typing. A send that
    # works has no business doing that, so the wipe is a retry-only measure.
    events = []
    deliver([FakeWriter(events)], monkeypatch)
    assert "clear" not in events


def test_enter_false_writes_and_verifies_but_does_not_commit(monkeypatch):
    events = []
    _, (submitted, _) = deliver([FakeWriter(events)], monkeypatch, enter=False)
    assert submitted is False
    assert "commit" not in events


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------

def test_text_that_never_appears_is_not_reported_as_sent(monkeypatch):
    # The whole point: the transport accepted every byte and the prompt stayed
    # empty. Before verification this returned ok=true.
    events = []
    with pytest.raises(inject.DeliveryError):
        deliver([FakeWriter(events, shows=False)], monkeypatch)
    assert "commit" not in events, "nothing may be submitted unverified"


def test_verification_counts_occurrences_rather_than_presence(monkeypatch):
    # A session that compacted an hour ago still has "/compact" on screen. A
    # presence check would call this verified without a byte having landed.
    events = []
    stale = FakeWriter(events, shows=False, screen="… ❯ /compact\n⎿ Compacted")
    with pytest.raises(inject.DeliveryError):
        deliver([stale], monkeypatch)


def test_a_wrapped_paste_still_verifies():
    # The TUI wraps long input and paints escape sequences at the boundary, so
    # the comparison is made on text with escapes and whitespace removed.
    before = "❯ "
    after = "❯ Please run this ex\x1b[0m\nactly as written"
    assert inject._landed(before, after, inject._probe(
        "Please run this exactly as written"))


# ---------------------------------------------------------------------------
# Retrying
# ---------------------------------------------------------------------------

def test_three_attempts_then_a_failure_naming_all_three(monkeypatch, caplog):
    events = []
    w = FakeWriter(events, shows=False)
    with caplog.at_level(logging.WARNING, logger="awm.reflection.inject"):
        with pytest.raises(inject.DeliveryError) as err:
            deliver([w], monkeypatch)
    assert events.count("write:/compact") == 3
    for n in (1, 2, 3):
        assert f"attempt {n}" in str(err.value)
    assert sum("attempt" in r.getMessage() for r in caplog.records) >= 3


def test_a_retry_clears_the_prompt_first(monkeypatch):
    # Otherwise a half-landed paste from the failed attempt is concatenated onto
    # by the next one, and the session submits a mangled line.
    events = []
    with pytest.raises(inject.DeliveryError):
        deliver([FakeWriter(events, shows=False)], monkeypatch)
    assert events.index("clear") > events.index("write:/compact"), \
        "attempt 1 writes before any clear happens"
    assert events.count("clear") == 2, "attempts 2 and 3 clear, attempt 1 does not"


def test_a_transient_failure_is_crossed_and_said_out_loud(monkeypatch, caplog):
    events = []
    broken = FakeWriter(events, fail_on=("write",))
    healthy = FakeWriter(events)
    with caplog.at_level(logging.WARNING, logger="awm.reflection.inject"):
        _, (submitted, _) = deliver([broken, healthy], monkeypatch)
    assert submitted is True
    assert any("attempt 2" in r.getMessage() and "earlier attempts failed"
               in r.getMessage() for r in caplog.records), \
        "a send that only worked on a retry must be visible afterwards"


def test_each_attempt_detects_the_lane_again(monkeypatch):
    # Detection sits inside the loop, immediately before the write. A lane
    # decided once at the top and reused is a lane that can go stale between
    # deciding it and using it — which is exactly how a re-homed pty is missed.
    detects = []
    with pytest.raises(inject.DeliveryError):
        deliver([FakeWriter([], shows=False)], monkeypatch, detects=detects)
    assert detects == [4242, 4242, 4242]


def test_a_lane_that_cannot_even_be_detected_is_retried(monkeypatch):
    calls = []

    def detect(pid):
        calls.append(pid)
        raise session_target.ResolveError("roster is mid-rewrite")

    monkeypatch.setattr(inject, "_open_lane",
                        lambda *a, **k: pytest.fail("must not open a lane"))
    with pytest.raises(inject.DeliveryError):
        inject.deliver(4242, "/compact", detect=detect)
    assert len(calls) == 3


# ---------------------------------------------------------------------------
# The commit boundary
# ---------------------------------------------------------------------------

def test_nothing_is_retried_once_enter_has_gone_in(monkeypatch, caplog):
    # Everything before Enter is safe to repeat because nothing was submitted.
    # Once Enter is on the wire the command may already be running, and `send`
    # carries arbitrary text — running it twice is worse than not at all.
    events = []
    w = FakeWriter(events, fail_on=("commit",))
    with caplog.at_level(logging.ERROR, logger="awm.reflection.inject"):
        with pytest.raises(inject.CommitFailed):
            deliver([w], monkeypatch)
    assert events.count("commit") == 1, "a commit failure must not be retried"
    assert any("did not complete" in r.getMessage() for r in caplog.records)


def test_a_commit_failure_is_a_delivery_error_for_the_caller():
    # So the adapter's one seam catches it like any other failure rather than
    # letting it escape as an unhandled exception.
    assert issubclass(inject.CommitFailed, inject.DeliveryError)
