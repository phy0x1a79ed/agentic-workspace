"""Scope claims and the busy-scope queue (scopes.md R3, R41, R42, R46)."""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

from awm.scopes import claims
from awm.scopes.hub_adapter import HANDLERS

pytestmark = [pytest.mark.scopes]

S1 = "cc" + "1" * 32
S2 = "cc" + "2" * 32
S3 = "cc" + "3" * 32


@pytest.fixture
def notices(monkeypatch, scopes_workspace):
    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(claims, "notify", lambda job, text: sent.append((job, text)) or True)
    monkeypatch.setattr(claims, "waiting_sessions", lambda: set())
    return sent


@pytest.fixture
def child():
    """A live process that is not this one, ended at will."""
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    yield proc
    proc.kill()
    proc.wait()


def _claim(**args):
    return HANDLERS["scope_claim"](args)


def test_a_free_scope_is_claimed(notices):
    out = _claim(project="p", scope="s", session_id=S1, pid=os.getpid())
    assert out["ok"] and out["claimed"] and out["holder"]["session_id"] == S1
    assert claims.live_holder("p", "s")["session_id"] == S1


def test_a_second_live_session_is_refused_with_the_holder(notices):
    _claim(project="p", scope="s", session_id=S1, pid=os.getpid())
    out = _claim(project="p", scope="s", session_id=S2, pid=os.getpid())
    assert out["ok"] is False and out["holder"]["session_id"] == S1 and "queue=true" in out["reason"]


def test_claiming_again_from_the_holder_is_fine(notices):
    _claim(project="p", scope="s", session_id=S1, pid=os.getpid())
    assert _claim(project="p", scope="s", session_id=S1, pid=os.getpid())["claimed"] is True


def test_the_claim_dies_with_the_session(notices, child):
    _claim(project="p", scope="s", session_id=S1, pid=child.pid)
    child.kill()
    child.wait()
    assert claims.live_holder("p", "s") is None
    assert _claim(project="p", scope="s", session_id=S2, pid=os.getpid())["claimed"] is True


def test_a_waiting_session_keeps_its_claim(notices, child, monkeypatch):
    _claim(project="p", scope="s", session_id=S1, pid=child.pid)
    child.kill()
    child.wait()
    monkeypatch.setattr(claims, "waiting_sessions", lambda: {S1})
    assert claims.live_holder("p", "s")["session_id"] == S1


def test_a_reused_pid_does_not_hold_the_claim(notices, monkeypatch):
    _claim(project="p", scope="s", session_id=S1, pid=os.getpid())
    monkeypatch.setattr(claims, "proc_start", lambda pid: "another-boot")
    assert claims.live_holder("p", "s") is None


def test_a_terminal_session_is_warned_not_blocked(notices):
    _claim(project="p", scope="s", session_id=S1, pid=os.getpid())
    out = _claim(project="p", scope="s", session_id=S2, pid=os.getpid(), terminal=True)
    assert out["ok"] is True and out["claimed"] is False and "not blocked" in out["warning"]
    assert claims.live_holder("p", "s")["session_id"] == S1


def test_the_queue_is_first_come_first_served(notices):
    _claim(project="p", scope="s", session_id=S1, pid=os.getpid())
    first = _claim(project="p", scope="s", session_id=S2, pid=os.getpid(), job="job2", queue=True)
    second = _claim(project="p", scope="s", session_id=S3, pid=os.getpid(), job="job3", queue=True)
    again = _claim(project="p", scope="s", session_id=S2, pid=os.getpid(), job="job2", queue=True)
    assert (first["queued_position"], second["queued_position"], again["queued_position"]) == (1, 2, 1)

    out = HANDLERS["scope_release"]({"project": "p", "scope": "s", "session_id": S1})
    assert out["released"] and out["next"]["session_id"] == S2 and out["notified"]
    assert notices[0][0] == "job2" and "p/s" in notices[0][1]

    # The second in line cannot jump the queue while the first is live.
    jump = _claim(project="p", scope="s", session_id=S3, pid=os.getpid())
    assert jump["claimed"] is False and "first" in jump["reason"]
    assert _claim(project="p", scope="s", session_id=S2, pid=os.getpid())["claimed"] is True
    assert [w["session_id"] for w in claims.listing("p", "s")["claims"][0]["queue"]] == [S3]


def test_the_head_is_notified_once(notices):
    _claim(project="p", scope="s", session_id=S1, pid=os.getpid())
    _claim(project="p", scope="s", session_id=S2, pid=os.getpid(), job="job2", queue=True)
    HANDLERS["scope_release"]({"project": "p", "scope": "s", "session_id": S1})
    HANDLERS["scope_release"]({"project": "p", "scope": "s", "session_id": S1})
    _claim(project="p", scope="s", session_id=S3, pid=os.getpid())
    assert len(notices) == 1


def test_a_dead_waiter_leaves_the_queue(notices, child):
    _claim(project="p", scope="s", session_id=S1, pid=os.getpid())
    _claim(project="p", scope="s", session_id=S2, pid=child.pid, job="job2", queue=True)
    child.kill()
    child.wait()
    out = HANDLERS["scope_release"]({"project": "p", "scope": "s", "session_id": S1})
    assert out["next"] is None and not notices


def test_withdraw_leaves_the_queue(notices):
    _claim(project="p", scope="s", session_id=S1, pid=os.getpid())
    _claim(project="p", scope="s", session_id=S2, pid=os.getpid(), queue=True)
    assert HANDLERS["scope_withdraw"]({"project": "p", "scope": "s", "session_id": S2})["withdrawn"]
    assert claims.listing("p", "s")["claims"][0]["queue"] == []


def test_release_by_another_session_changes_nothing(notices):
    _claim(project="p", scope="s", session_id=S1, pid=os.getpid())
    out = HANDLERS["scope_release"]({"project": "p", "scope": "s", "session_id": S2})
    assert out["released"] is False and claims.live_holder("p", "s")["session_id"] == S1


def test_the_caller_pid_stands_in_for_pid(notices):
    out = _claim(project="p", scope="s", session_id=S1, _caller_pid=os.getpid())
    assert out["holder"]["pid"] == os.getpid() and claims.live_holder("p", "s")


def test_claims_lists_holders_and_queues(notices):
    _claim(project="p", scope="a", session_id=S1, pid=os.getpid())
    _claim(project="p", scope="b", session_id=S2, pid=os.getpid())
    out = HANDLERS["scope_claims"]({"project": "p"}, None)
    assert out["count"] == 2 and {c["scope"] for c in out["claims"]} == {"a", "b"}


def test_bump_counts_up(scopes_workspace):
    assert [claims.bump("creates") for _ in range(3)] == [1, 2, 3]
