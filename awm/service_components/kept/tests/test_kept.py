"""The shared resume policy, on a fake clock."""

from __future__ import annotations

import json

from awm.kept import (BACKOFF_S, BUDGET, DECAY_S, HOLD_S, SETTLE_S, Action, Breaker, Outage,
                      Situation, decide, transcript_tail)

T0 = 1_792_000_000.0


def test_a_node_without_a_daemon_is_waited_for_and_spends_no_budget():
    b = Breaker()
    for state in ("exited", "missing", "failed"):
        action, why = decide(Situation(state, daemon_up=False), b, T0)
        assert action is Action.WAIT and "daemon" in why
    assert decide(Situation("exited", node_reachable=False), b, T0)[0] is Action.WAIT
    assert b.attempts == []


def test_an_exited_session_is_resumed_then_backed_off():
    b = Breaker()
    assert decide(Situation("exited"), b, T0)[0] is Action.RESUME
    b.attempt(T0, "resume")
    assert decide(Situation("exited"), b, T0 + 1)[0] is Action.BACKOFF
    assert decide(Situation("exited"), b, T0 + BACKOFF_S[0])[0] is Action.RESUME


def test_backoff_grows_one_two_five_ten_minutes():
    b = Breaker()
    waits = []
    for i in range(3):
        b.attempt(T0 + i, "resume")
        waits.append(b.next_try - (T0 + i))
    assert waits == list(BACKOFF_S[:3])


def test_three_resumes_that_do_not_stay_up_turn_into_a_fresh_start():
    b = Breaker()
    for i in range(3):
        b.attempt(T0 + i * 1000, "resume")
    action, why = decide(Situation("exited"), b, b.next_try)
    assert action is Action.FRESH and "resumes" in why


def test_the_budget_trips_the_breaker_and_it_holds():
    b = Breaker()
    for i in range(BUDGET):
        b.attempt(T0 + i * 700, "resume" if i < 3 else "fresh")
    now = T0 + BUDGET * 700
    assert b.tripped(now)
    assert decide(Situation("exited"), b, now)[0] is Action.GIVE_UP
    assert decide(Situation("exited"), b, b.tripped_at + HOLD_S)[0] is Action.RESUME


def test_old_attempts_decay():
    b = Breaker()
    for i in range(BUDGET - 1):
        b.attempt(T0 + i, "resume")
    b.attempt(T0 + DECAY_S + 10, "resume")
    assert not b.tripped(T0 + DECAY_S + 11)


def test_a_session_that_stays_up_clears_the_breaker():
    b = Breaker()
    b.attempt(T0, "resume")
    assert not b.healthy(T0 + 60, alive_since=T0 + 5)
    assert b.healthy(T0 + SETTLE_S + 10, alive_since=T0 + 5)
    assert b.attempts == [] and b.next_try == 0.0


def test_failed_missing_and_changed_specs_start_fresh():
    b = Breaker()
    assert decide(Situation("failed"), b, T0)[0] is Action.FRESH
    assert decide(Situation("missing"), b, T0)[0] is Action.FRESH
    assert decide(Situation("exited", spec_changed=True), b, T0)[0] is Action.FRESH
    assert decide(Situation("exited", resumable=False), b, T0)[0] is Action.FRESH


def test_alive_and_blocked_need_nothing():
    b = Breaker()
    assert decide(Situation("alive"), b, T0)[0] is Action.NONE
    assert decide(Situation("blocked"), b, T0)[0] is Action.NONE


def test_breaker_round_trips_through_json():
    b = Breaker()
    b.attempt(T0, "resume")
    again = Breaker.from_dict(json.loads(json.dumps(b.to_dict())))
    assert again == b


def test_outage_renders_cause_span_and_missed():
    o = Outage(down_at=T0, cause="killed", up_at=T0 + 3 * 3600 + 120, missed=4, action="resumed")
    text = o.render(node="altair", what_now="Read them with `social history`.")
    assert "on altair because the Claude Code daemon on its node was killed" in text
    assert "(3 h 2 min)" in text and "4 messages arrived meanwhile." in text
    assert "resumed with its own conversation" in text
    assert text.endswith("Do not announce the restart in the chat.")
    assert Outage.from_dict(o.to_dict()) == o
    assert Outage.from_dict(None) is None


def test_transcript_tail_keeps_what_was_said():
    lines = [
        json.dumps({"type": "user", "message": {"content": "hello"}}),
        json.dumps({"type": "assistant", "message": {"content": [
            {"type": "text", "text": "hi there"}, {"type": "tool_use", "name": "Bash"}]}}),
        json.dumps({"type": "user", "message": {"content": [{"type": "tool_result"}]}}),
        "not json",
        json.dumps({"type": "summary", "summary": "x"}),
    ]
    assert transcript_tail(lines) == "[user] hello\n\n[assistant] hi there"
    assert transcript_tail(lines, turns=1) == "[assistant] hi there"
