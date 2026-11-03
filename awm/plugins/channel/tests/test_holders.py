import pytest

from awm.channel import holders
from awm.channel.holders import ChannelError

pytestmark = [pytest.mark.unit, pytest.mark.smoke]


def test_a_call_with_no_session_behind_it_cannot_hold_a_channel():
    with pytest.raises(ChannelError, match="running Claude Code session"):
        holders.caller({}, None)


def test_only_a_background_session_holds_a_channel(monkeypatch):
    monkeypatch.setattr(holders, "_session_claim",
                        lambda pid: {"kind": "interactive", "job": "", "name": "x"})
    with pytest.raises(ChannelError, match="background session"):
        holders.caller({"_caller_pid": 42}, None)


def test_a_background_session_is_named_by_its_node_and_job(monkeypatch):
    monkeypatch.setattr(holders, "_session_claim",
                        lambda pid: {"kind": "bg", "job": "abcd1234", "name": "worker"})
    monkeypatch.setattr(holders.config, "node_name", lambda: "mizar")
    assert holders.caller({"_caller_pid": 42}, None) == {
        "node": "mizar", "job": "abcd1234", "name": "worker"}


def test_the_plugin_does_not_reach_into_social():
    from pathlib import Path

    src = Path(holders.__file__).parent
    for py in src.glob("*.py"):
        assert "awm.social" not in py.read_text(), py
