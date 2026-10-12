"""Owner validation through the scopes service's resolveScope RPC."""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.journal]


@pytest.fixture()
def real_owners(monkeypatch):
    """Undo the suite-wide stub so the real check runs against a fake gateway."""
    import importlib
    from awm.journal import owners
    importlib.reload(owners)
    owners._known.clear()
    import awm.journal.channel as channel
    monkeypatch.setattr(channel, "owners", owners)
    return owners


def _gateway(monkeypatch, reply=None, exc=None):
    calls = []

    def call_sync(service, fn, args=None, **kw):
        calls.append((service, fn, args))
        if exc:
            raise exc
        return reply

    monkeypatch.setattr("awm.gatewayclient.call_sync", call_sync)
    return calls


def test_a_post_to_an_unknown_scope_is_refused(journal_workspace, real_owners, monkeypatch):
    from awm.journal import channel
    calls = _gateway(monkeypatch, {"exists": False, "project": "awm", "scope": "nope"})
    with pytest.raises(ValueError, match="no scope awm/nope"):
        channel.post("awm", "nope", author="user:a", body="x")
    assert calls == [("scopes", "resolveScope", {"project": "awm", "scope": "nope"})]


def test_a_known_scope_is_accepted_and_remembered(journal_workspace, real_owners, monkeypatch):
    from awm.journal import channel
    calls = _gateway(monkeypatch, {"exists": True, "status": "retired"})
    channel.post("awm", "dev", author="user:a", body="one")
    channel.post("awm", "dev", author="user:a", body="two")
    assert len(calls) == 1


def test_a_non_literal_channel_is_not_checked(journal_workspace, real_owners, monkeypatch):
    from awm.journal import channel
    calls = _gateway(monkeypatch, {"exists": False})
    channel.post("", "user:alice", author="agent:awm/dev", body="for alice")
    assert calls == []


def test_an_unreachable_scopes_service_does_not_stop_a_post(journal_workspace, real_owners, monkeypatch):
    from awm.journal import channel
    _gateway(monkeypatch, exc=RuntimeError("gateway down"))
    assert channel.post("awm", "dev", author="user:a", body="written anyway").body == "written anyway"


def test_goals_on_an_unknown_scope_are_refused_too(journal_workspace, real_owners, monkeypatch):
    from awm.journal import goals
    _gateway(monkeypatch, {"exists": False})
    with pytest.raises(ValueError):
        goals.set_goal(objective="x", author="user:tony", level="scope", project="awm", scope="nope")


def test_siblings_exclude_the_scope_itself(real_owners, monkeypatch):
    calls = _gateway(monkeypatch, {"scopes": [{"scope": "dev"}, {"scope": "a"}, {"scope": "mine"}]})
    assert real_owners.scope_siblings("awm", "mine") == ["dev", "a"]
    assert calls[0][:2] == ("scopes", "scope_search")
