"""Being told, and noticing when we have stopped being told.

The stream is the whole of why a saved paper appears in seconds. Its failure
mode is not a crash: it is a socket that stays open while the subscription
behind it is gone, which looks identical to a quiet library. So most of what is
asserted here is about deafness rather than about delivery.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from awm.zotero import stream as stream_mod

pytestmark = [pytest.mark.unit, pytest.mark.smoke]


class FakeSocket:
    """A websocket that hands over a scripted set of frames, then stalls."""

    def __init__(self, frames: list[dict], *, stall: bool = True) -> None:
        self.frames = [json.dumps(f) for f in frames]
        self.sent: list[dict] = []
        self.stall = stall

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def recv(self) -> str:
        if self.frames:
            return self.frames.pop(0)
        if self.stall:
            await asyncio.sleep(3600)
        raise ConnectionError("closed")

    async def send(self, raw: str) -> None:
        self.sent.append(json.loads(raw))


@pytest.fixture
def connect(monkeypatch):
    """Hand `Stream` a socket of our choosing, and a key so it does not idle."""
    monkeypatch.setattr(stream_mod.source, "KEY", "a-key")
    made: list[FakeSocket] = []

    def _use(sock: FakeSocket):
        monkeypatch.setattr(stream_mod.websockets, "connect",
                            lambda *a, **k: sock)
        made.append(sock)
        return sock
    return _use


# -- reading a topic ---------------------------------------------------------


@pytest.mark.parametrize("topic,expected", [
    ("/users/5331043", "users/5331043"),
    ("/groups/5284390", "groups/5284390"),
    # A feed rather than a library. The mirror does not read it, and guessing
    # would send a pass looking for a library that does not exist.
    ("/users/5331043/publications", ""),
    ("/somethingnew/1", ""),
    ("", ""),
    ("/", ""),
])
def test_only_a_library_topic_is_read_as_one(topic, expected):
    assert stream_mod.library_of(topic) == expected


# -- one session -------------------------------------------------------------


def test_a_change_is_reported_with_its_library_and_version(connect):
    seen: list[tuple[str, int]] = []
    s = stream_mod.Stream(lambda lib, v: _record(seen, lib, v))
    sock = connect(FakeSocket([
        {"event": "connected", "retry": 10000},
        {"event": "subscriptionsCreated",
         "subscriptions": [{"apiKey": "a-key",
                            "topics": ["/users/1", "/groups/2"]}],
         "errors": []},
        {"event": "topicUpdated", "topic": "/groups/2", "version": 211},
    ], stall=False))

    with pytest.raises(ConnectionError):
        asyncio.run(s._session())

    assert sock.sent == [{"action": "createSubscriptions",
                          "subscriptions": [{"apiKey": "a-key"}]}]
    assert seen == [("groups/2", 211)]
    assert s.topics == ["/users/1", "/groups/2"]
    assert s.changes == 1


def test_the_publications_feed_does_not_start_a_pass(connect):
    seen: list[tuple[str, int]] = []
    s = stream_mod.Stream(lambda lib, v: _record(seen, lib, v))
    connect(FakeSocket([
        {"event": "connected", "retry": 10000},
        {"event": "topicUpdated", "topic": "/users/1/publications",
         "version": 9},
    ], stall=False))
    with pytest.raises(ConnectionError):
        asyncio.run(s._session())
    assert seen == [] and s.changes == 0


def test_a_refused_subscription_is_recorded_rather_than_waited_out(connect):
    """A key that reaches nothing subscribes to nothing and then sits in
    perfect health for ever. The error has to be visible in `status`."""
    s = stream_mod.Stream(lambda lib, v: _record([], lib, v))
    connect(FakeSocket([
        {"event": "connected", "retry": 10000},
        {"event": "subscriptionsCreated", "subscriptions": [],
         "errors": [{"apiKey": "a-key", "error": "Invalid API key"}]},
    ], stall=False))
    with pytest.raises(ConnectionError):
        asyncio.run(s._session())
    assert "Invalid API key" in (s.last_error or "")
    assert s.health["last_error"]


def test_the_servers_own_retry_is_honoured(connect):
    s = stream_mod.Stream(lambda lib, v: _record([], lib, v))
    connect(FakeSocket([{"event": "connected", "retry": 4500}], stall=False))
    with pytest.raises(ConnectionError):
        asyncio.run(s._session())
    # The delay is only returned on a clean end, so drive one: a group joining
    # ends the session so the subscription can be rebuilt.
    connect(FakeSocket([{"event": "connected", "retry": 4500},
                        {"event": "topicAdded", "topic": "/groups/3"}]))
    assert asyncio.run(s._session()) == pytest.approx(4.5)


def test_a_silent_stream_is_rebuilt_rather_than_trusted(connect, monkeypatch):
    """Keepalives only prove the far end is alive. A subscription lost behind a
    healthy socket looks exactly like a library nobody is touching."""
    monkeypatch.setattr(stream_mod, "IDLE_S", 0.01)
    s = stream_mod.Stream(lambda lib, v: _record([], lib, v))
    connect(FakeSocket([{"event": "connected", "retry": 10000}], stall=True))
    assert asyncio.run(s._session()) == pytest.approx(10.0)
    assert s.connected_at is None


def test_health_says_how_long_it_has_been_quiet(connect):
    s = stream_mod.Stream(lambda lib, v: _record([], lib, v))
    assert s.health["connected"] is False
    connect(FakeSocket([{"event": "connected", "retry": 10000},
                        {"event": "topicAdded", "topic": "/groups/3"}]))
    asyncio.run(s._session())
    assert s.connections == 1
    assert s.health["silent_for_s"] is not None
    assert s.health["url"] == stream_mod.URL


async def _record(into: list, library: str, version: int) -> None:
    into.append((library, version))
