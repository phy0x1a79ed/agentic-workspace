import pytest

from awm.channel import holders
from awm.channel.holders import ChannelError

pytestmark = [pytest.mark.unit, pytest.mark.smoke]

SID = "cc" + "0123456789abcdef" * 2


def test_a_call_with_no_session_behind_it_cannot_hold_a_channel():
    with pytest.raises(ChannelError, match="running agent session"):
        holders.caller({}, None)


def test_only_a_background_session_holds_a_channel(monkeypatch):
    monkeypatch.setattr(holders, "_record", lambda pid: {
        "background": False, "job": "", "session": SID, "name": "x"})
    with pytest.raises(ChannelError, match="background session"):
        holders.caller({"_caller_pid": 42}, None)


def test_a_session_on_this_node_is_named_by_its_r9_id(monkeypatch):
    monkeypatch.setattr(holders, "_record", lambda pid: {
        "background": True, "job": "abcd1234", "session": SID, "name": "worker"})
    monkeypatch.setattr(holders.config, "node_name", lambda: "mizar")
    assert holders.caller({"_caller_pid": 42}, None) == {
        "node": "mizar", "session": SID, "job": "abcd1234", "name": "worker", "pid": 42}


def test_the_session_id_the_gateway_sends_wins(monkeypatch):
    monkeypatch.setattr(holders, "_job_of_session", lambda sid: "abcd1234")
    monkeypatch.setattr(holders.config, "node_name", lambda: "mizar")
    holder = holders.caller({"_caller_session_id": SID}, None)
    assert holder["session"] == SID and holder["job"] == "abcd1234"


def test_a_session_id_with_no_job_behind_it_cannot_hold_a_channel(monkeypatch):
    """An isolated session has no job to type into yet (R111)."""
    monkeypatch.setattr(holders, "_job_of_session", lambda sid: None)
    monkeypatch.setattr(holders.config, "node_name", lambda: "mizar")
    with pytest.raises(ChannelError, match="background session"):
        holders.caller({"_caller_session_id": SID}, None)


def test_a_holder_on_another_node_with_no_session_id_is_known_by_its_job(monkeypatch):
    from awm.config import edges

    monkeypatch.setattr(edges, "caller_peer", lambda as_: "alcor")
    holder = holders.caller({"_caller_session": {"kind": "bg", "job": "feed1234",
                                                 "name": "far"}}, "peer:alcor")
    assert holder == {"node": "alcor", "session": "job:feed1234", "job": "feed1234",
                      "name": "far"}


def test_the_plugin_does_not_reach_into_social():
    from pathlib import Path

    src = Path(holders.__file__).parent
    for py in src.rglob("*.py"):
        assert "awm.social" not in py.read_text(), py


def test_a_record_from_reflection_session_of_names_the_holder(monkeypatch):
    """The shape redux-harness's ``gatewayclient.sessions.session_of`` returns."""
    import sys
    import types

    fake = types.ModuleType("awm.gatewayclient.sessions")
    fake.session_of = lambda pid: {"session_id": SID, "job": "abcd1234",
                                   "hosting": "background", "name": "worker"}
    monkeypatch.setitem(sys.modules, "awm.gatewayclient.sessions", fake)
    import awm.gatewayclient as gc
    monkeypatch.setattr(gc, "sessions", fake, raising=False)
    assert holders._record(42) == {"background": True, "job": "abcd1234",
                                   "session": SID, "name": "worker"}


ROWS = {"ok": True, "sessions": [
    {"session_id": SID, "job": "abcd1234", "harness": "cch", "state": "working"},
    {"session_id": "cc" + "f" * 32, "job": "dead0000", "harness": "cch", "state": "gone"},
    {"session_id": "dsh" + "1" * 8, "job": "", "harness": "isolated", "live": False},
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
    assert holders.live_sessions() == {SID}
    assert seen == [("reflection", "list_other", {})]


def test_the_job_of_a_session_is_its_list_other_row(monkeypatch):
    seen = _list_other(monkeypatch, ROWS)
    assert holders._job_of_session(SID) == "abcd1234"
    assert holders._job_of_session("cc" + "e" * 32) is None
    assert seen[0][2] == {"include_stopped": True}


@pytest.mark.parametrize("answer", [
    RuntimeError("reflection is down"),
    {"ok": True, "sessions": [{"job": "abcd1234", "state": "working"}]},
    {"ok": True, "sessions": ROWS["sessions"], "errors": [{"harness": "dsh"}]},
])
def test_a_list_other_that_cannot_tell_names_no_job_and_closes_nothing(monkeypatch, answer):
    _list_other(monkeypatch, answer)
    assert holders._session_rows() is None
    assert holders._job_of_session(SID) is None
    assert holders.live_sessions() is None
