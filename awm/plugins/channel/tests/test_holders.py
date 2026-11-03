import pytest

from awm.channel import holders
from awm.channel.holders import ChannelError

pytestmark = [pytest.mark.unit, pytest.mark.smoke]

SID = "cc" + "0123456789abcdef" * 2
UUID = "01234567-89ab-cdef-0123-456789abcdef"


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
    from awm.config import peerbook

    monkeypatch.setattr(peerbook, "caller_peer", lambda as_: "alcor")
    holder = holders.caller({"_caller_session": {"kind": "bg", "job": "feed1234",
                                                 "name": "far"}}, "peer:alcor")
    assert holder == {"node": "alcor", "session": "job:feed1234", "job": "feed1234",
                      "name": "far"}


def test_r9_is_cc_and_the_uuid_without_dashes():
    assert holders.r9(UUID) == SID
    assert holders.r9("not-a-uuid") is None


def test_the_plugin_does_not_reach_into_social():
    from pathlib import Path

    src = Path(holders.__file__).parent
    for py in src.rglob("*.py"):
        assert "awm.social" not in py.read_text(), py
