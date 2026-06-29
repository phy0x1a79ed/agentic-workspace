"""Engine policy tests — single-approve / burst-hold / approve-all / dedup /
hold-TTL — driven by a fake Duo client (no network)."""

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
    params = dict(dedup_seconds=3.0, approve_all_minutes=5.0,
                  burst_threshold=1, hold_ttl_seconds=120.0)
    params.update(kw)
    return ApprovalEngine(client, NULL_NOTIFIER, **params)


@pytest.mark.smoke
def test_single_login_auto_approves():
    c = FakeClient()
    eng = make_engine(c)
    eng.handle_transactions([tx("u1")])
    assert c.answered == [("u1", "approve")]
    assert eng.approved_count == 1
    assert eng.held_transactions() == []


@pytest.mark.smoke
def test_burst_holds_without_approving():
    c = FakeClient()
    eng = make_engine(c)
    eng.handle_transactions([tx("u1"), tx("u2")])
    assert c.answered == []
    assert {t.urgid for t in eng.held_transactions()} == {"u1", "u2"}
    assert eng.approved_count == 0


@pytest.mark.smoke
def test_approve_and_deny_held():
    c = FakeClient()
    eng = make_engine(c)
    eng.handle_transactions([tx("u1"), tx("u2")])
    assert eng.approve("u1") is True
    assert ("u1", "approve") in c.answered
    assert eng.deny("u2") is True
    assert ("u2", "deny") in c.answered
    assert eng.held_transactions() == []
    # Unknown / already-handled ids resolve to False, not an error.
    assert eng.approve("u1") is False
    assert eng.deny("nope") is False


@pytest.mark.smoke
def test_approve_all_clears_and_opens_window():
    c = FakeClient()
    eng = make_engine(c)
    eng.handle_transactions([tx("u1"), tx("u2")])
    assert eng.approve_all() == 2
    assert eng.held_transactions() == []
    assert eng.approve_all_remaining() > 0
    # Inside the window a fresh single login is auto-approved immediately.
    eng.handle_transactions([tx("u3")])
    assert ("u3", "approve") in c.answered


@pytest.mark.smoke
def test_dedup_suppresses_repeat_within_window():
    c = FakeClient()
    eng = make_engine(c)
    eng.handle_transactions([tx("u1")])
    eng.handle_transactions([tx("u1")])  # same urgid, immediately
    assert c.answered.count(("u1", "approve")) == 1


@pytest.mark.smoke
def test_held_transactions_expire_after_ttl():
    c = FakeClient()
    eng = make_engine(c, hold_ttl_seconds=0.01)
    eng.handle_transactions([tx("u1"), tx("u2")])
    assert len(eng.held_transactions()) == 2
    time.sleep(0.05)
    eng.handle_transactions([])  # next fetch expires the stale holds
    assert eng.held_transactions() == []


@pytest.mark.smoke
def test_failed_reply_is_not_counted_as_approved():
    c = FakeClient()
    c.fail_urgids = {"u1"}
    eng = make_engine(c)
    eng.handle_transactions([tx("u1")])
    assert eng.approved_count == 0
    assert c.answered == []
