"""Who holds a channel or a seat, read from the gateway's stamps and reflection's answers."""
from __future__ import annotations

import pytest

from awm.gatewayclient import holders, sessions
from awm.gatewayclient.holders import HolderError

SID = "cc" + "0123456789abcdef" * 2
ISO = "cc" + "abcdef0123456789" * 2


def _record(monkeypatch, rec):
    monkeypatch.setattr(sessions, "session_of", lambda pid: rec)


def test_a_call_with_no_session_behind_it_holds_nothing():
    with pytest.raises(HolderError, match="running agent session"):
        holders.caller({}, None)


def test_the_refusal_names_what_is_held():
    with pytest.raises(HolderError, match="no calling session reached a seat"):
        holders.caller({}, None, what="a seat")


def test_an_interactive_session_holds_nothing(monkeypatch):
    _record(monkeypatch, {"hosting": "terminal", "job": "", "session_id": SID, "name": "x"})
    with pytest.raises(HolderError, match="background session"):
        holders.caller({"_caller_pid": 42}, None)


def test_a_session_on_this_node_is_named_by_its_r9_id(monkeypatch):
    _record(monkeypatch, {"hosting": "background", "job": "abcd1234",
                          "session_id": SID, "name": "worker"})
    monkeypatch.setattr(holders.config, "node_name", lambda: "mizar")
    assert holders.caller({"_caller_pid": 42}, None) == {
        "node": "mizar", "session": SID, "job": "abcd1234", "name": "worker", "pid": 42}


def test_the_session_id_the_gateway_sends_wins(monkeypatch):
    monkeypatch.setattr(holders, "_push_target", lambda sid: "abcd1234")
    monkeypatch.setattr(holders.config, "node_name", lambda: "mizar")
    holder = holders.caller({"_caller_session_id": SID}, None)
    assert holder["session"] == SID and holder["job"] == "abcd1234"


def test_a_session_id_with_nothing_to_type_into_holds_nothing(monkeypatch):
    monkeypatch.setattr(holders, "_push_target", lambda sid: None)
    monkeypatch.setattr(holders.config, "node_name", lambda: "mizar")
    with pytest.raises(HolderError, match="background session"):
        holders.caller({"_caller_session_id": SID}, None)


def test_a_holder_on_another_node_with_no_session_id_is_known_by_its_job(monkeypatch):
    from awm.config import edges

    monkeypatch.setattr(edges, "caller_peer", lambda as_: "alcor")
    holder = holders.caller({"_caller_session": {"kind": "bg", "job": "feed1234",
                                                 "name": "far"}}, "peer:alcor")
    assert holder == {"node": "alcor", "session": "job:feed1234", "job": "feed1234",
                      "name": "far"}


def test_a_borrower_forwards_its_record_as_a_claim(monkeypatch):
    _record(monkeypatch, {"hosting": "background", "job": "abcd1234",
                          "session_id": SID, "name": "worker"})
    assert holders.claim(42) == {"kind": "bg", "job": "abcd1234", "name": "worker",
                                 "session_id": SID}
    _record(monkeypatch, None)
    assert holders.claim(42) is None


ROWS = {"ok": True, "sessions": [
    {"session_id": SID, "job": "abcd1234", "harness": "cch", "state": "working"},
    {"session_id": "cc" + "f" * 32, "job": "dead0000", "harness": "cch", "state": "gone"},
    {"session_id": ISO, "job": None, "kind": "isolated", "live": True},
    {"session_id": "cc" + "e" * 32, "job": None, "kind": "interactive", "live": True},
]}


def _list_other(monkeypatch, answer):
    seen = []

    def call_sync(service, verb, args, timeout):
        seen.append((service, verb, args))
        if isinstance(answer, Exception):
            raise answer
        return answer
    monkeypatch.setattr(holders.gatewayclient, "call_sync", call_sync)
    return seen


def test_live_sessions_are_the_live_rows_of_list_other(monkeypatch):
    seen = _list_other(monkeypatch, ROWS)
    assert holders.live_sessions() == {SID, ISO, "cc" + "e" * 32}
    assert seen == [("reflection", "list_other", {})]


def test_a_push_reaches_a_session_by_its_job(monkeypatch):
    seen = _list_other(monkeypatch, ROWS)
    assert holders._push_target(SID) == "abcd1234"
    assert holders._push_target("cc" + "e" * 32) is None
    assert holders._push_target("cc" + "1" * 32) is None
    assert seen[0][2] == {"include_stopped": True}


def test_an_isolated_session_holds_a_channel_by_its_r9_id(monkeypatch):
    """It has no job, and reflection types into it by its session id (R111)."""
    _list_other(monkeypatch, ROWS)
    monkeypatch.setattr(holders.config, "node_name", lambda: "mizar")
    holder = holders.caller({"_caller_session_id": ISO.upper()}, None)
    assert holder == {"node": "mizar", "session": ISO, "job": ISO, "name": ""}


@pytest.mark.parametrize("answer", [
    RuntimeError("reflection is down"),
    {"ok": True, "sessions": [{"job": "abcd1234", "state": "working"}]},
    {"ok": True, "sessions": ROWS["sessions"], "errors": [{"harness": "dsh"}]},
])
def test_a_list_other_that_cannot_tell_names_nothing_and_closes_nothing(monkeypatch, answer):
    _list_other(monkeypatch, answer)
    assert holders._session_rows() is None
    assert holders._push_target(SID) is None
    assert holders.live_sessions() is None
