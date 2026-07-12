"""Engine policy tests — budget-driven approve / overflow-hold / retro-promote /
approve-all / dedup / hold-TTL — driven by a fake Duo client (no network)."""

from __future__ import annotations

import time

import pytest

from awm.twofa.duo import Transaction
from awm.twofa.engine import ApprovalEngine
from awm.twofa.notify import NULL_NOTIFIER


class FakeClient:
    def __init__(self) -> None:
        self.host = "api-test.duosecurity.com"
        self.answered: list[tuple[str, str]] = []
        self.fail_urgids: set[str] = set()

    def reply_transaction(self, urgid: str, answer: str) -> dict:
        if urgid in self.fail_urgids:
            raise RuntimeError("simulated Duo failure")
        self.answered.append((urgid, answer))
        return {"response": {"result": "success"}}

    def get_transactions(self) -> list[Transaction]:
        return []


def tx(urgid: str, app: str = "App") -> Transaction:
    return Transaction(urgid=urgid, raw={"integration_name": app})


def make_engine(client: FakeClient, **kw) -> ApprovalEngine:
    params = dict(dedup_seconds=3.0, approve_all_minutes=5.0, hold_ttl_seconds=120.0)
    params.update(kw)
    return ApprovalEngine(client, NULL_NOTIFIER, **params)


@pytest.mark.smoke
def test_single_login_auto_approves_within_budget():
    c = FakeClient()
    eng = make_engine(c)
    eng.grant(1)
    eng.handle_transactions([tx("u1")])
    assert c.answered == [("u1", "approve")]
    assert eng.approved_count == 1
    assert eng.budget_remaining() == 0
    assert eng.held_transactions() == []


@pytest.mark.smoke
def test_no_budget_holds_without_approving():
    """The old bug: pushes arrive but nothing is armed → hold, never approve."""
    c = FakeClient()
    eng = make_engine(c)  # no grant → budget 0
    eng.handle_transactions([tx("u1"), tx("u2")])
    assert c.answered == []
    assert {t.urgid for t in eng.held_transactions()} == {"u1", "u2"}
    assert eng.approved_count == 0


@pytest.mark.smoke
def test_n_overlapping_grants_approve_n():
    """The fix: N overlapping arms → N overlapping pushes all approve at once."""
    c = FakeClient()
    eng = make_engine(c)
    eng.grant(1)
    eng.grant(1)  # two overlapping connects → budget 2
    eng.handle_transactions([tx("u1"), tx("u2")])
    assert {a for a in c.answered} == {("u1", "approve"), ("u2", "approve")}
    assert eng.approved_count == 2
    assert eng.budget_remaining() == 0
    assert eng.held_transactions() == []


@pytest.mark.smoke
def test_over_budget_holds_overflow():
    """Budget 1, three pushes → oldest approved, the rest held for review."""
    c = FakeClient()
    eng = make_engine(c)
    eng.grant(1)
    eng.handle_transactions([tx("u1"), tx("u2"), tx("u3")])
    assert c.answered == [("u1", "approve")]
    assert eng.approved_count == 1
    assert eng.budget_remaining() == 0
    assert {t.urgid for t in eng.held_transactions()} == {"u2", "u3"}


@pytest.mark.smoke
def test_retro_promotion_on_later_grant():
    """A push held for lack of budget is approved once a later grant frees one,
    oldest-first."""
    c = FakeClient()
    eng = make_engine(c)
    eng.handle_transactions([tx("u1"), tx("u2")])  # budget 0 → both held
    assert eng.approved_count == 0

    eng.grant(1)
    eng.handle_transactions([tx("u1"), tx("u2")])  # oldest (u1) promoted
    assert ("u1", "approve") in c.answered
    assert {t.urgid for t in eng.held_transactions()} == {"u2"}

    eng.grant(1)
    eng.handle_transactions([tx("u2")])  # u1 now dedup-suppressed; u2 promoted
    assert ("u2", "approve") in c.answered
    assert eng.held_transactions() == []


@pytest.mark.smoke
def test_approve_and_deny_held():
    c = FakeClient()
    eng = make_engine(c)
    eng.handle_transactions([tx("u1"), tx("u2")])  # budget 0 → both held
    assert eng.approve("u1") is True
    assert ("u1", "approve") in c.answered
    assert eng.deny("u2") is True
    assert ("u2", "deny") in c.answered
    assert eng.held_transactions() == []
    # Unknown / already-handled ids resolve to False, not an error.
    assert eng.approve("u1") is False
    assert eng.deny("nope") is False


@pytest.mark.smoke
def test_approve_all_clears_and_does_not_consume_budget():
    c = FakeClient()
    eng = make_engine(c)
    eng.handle_transactions([tx("u1"), tx("u2")])  # budget 0 → both held
    assert eng.approve_all() == 2
    assert eng.held_transactions() == []
    assert eng.approve_all_remaining() > 0
    # A separate counted budget is left untouched by the approve-all window.
    eng.grant(1)
    eng.handle_transactions([tx("u3"), tx("u4")])  # both approved via approve-all
    assert ("u3", "approve") in c.answered
    assert ("u4", "approve") in c.answered
    assert eng.budget_remaining() == 1  # approve-all did not spend it


@pytest.mark.smoke
def test_social_style_large_budget_approves_all_pending():
    """The Discord /approve path arms a large count; every push approves."""
    c = FakeClient()
    eng = make_engine(c)
    eng.grant(1000)
    eng.handle_transactions([tx(f"u{i}") for i in range(5)])
    assert eng.approved_count == 5
    assert eng.budget_remaining() == 995
    assert eng.held_transactions() == []


@pytest.mark.smoke
def test_dedup_suppresses_repeat_within_window():
    c = FakeClient()
    eng = make_engine(c)
    eng.grant(2)
    eng.handle_transactions([tx("u1")])
    eng.handle_transactions([tx("u1")])  # same urgid, immediately
    assert c.answered.count(("u1", "approve")) == 1
    assert eng.budget_remaining() == 1  # the duplicate did not spend a second


@pytest.mark.smoke
def test_held_transactions_expire_after_ttl():
    c = FakeClient()
    eng = make_engine(c, hold_ttl_seconds=0.01)
    eng.handle_transactions([tx("u1"), tx("u2")])  # budget 0 → both held
    assert len(eng.held_transactions()) == 2
    time.sleep(0.05)
    eng.handle_transactions([])  # next fetch expires the stale holds
    assert eng.held_transactions() == []


@pytest.mark.smoke
def test_failed_reply_is_not_counted_or_charged():
    c = FakeClient()
    c.fail_urgids = {"u1"}
    eng = make_engine(c)
    eng.grant(1)
    eng.handle_transactions([tx("u1")])
    assert eng.approved_count == 0
    assert c.answered == []
    assert eng.budget_remaining() == 1  # a failed reply does not spend budget


@pytest.mark.smoke
def test_grant_and_clear_budget():
    eng = make_engine(FakeClient())
    assert eng.grant(3) == 3
    assert eng.budget_remaining() == 3
    assert eng.clear_budget() == 3
    assert eng.budget_remaining() == 0
