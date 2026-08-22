"""Tests for ``subscribe_peer`` — cross-peer streaming (federation T1).

``subscribe_peer`` is the peer analogue of ``subscribe``: it opens a WS to a
peer's httpsfront edge (CA-verified TLS + peer bearer) and yields decoded
frames. Two properties matter and are asserted here against fakes (no gateway,
no sockets, no real ssh):

1. **Decode parity** — a consumer must not be able to tell a peer stream from a
   local one, so ``subscribe_peer`` must yield exactly what ``subscribe`` yields
   for the same frame sequence (JSON text decoded, non-JSON text passed raw,
   binary frames skipped).
2. **Rotation reconnect** — the edge rejects a stale bearer *during the
   handshake* (``InvalidStatus`` 401/403), so ``subscribe_peer`` must re-fetch
   the credential once (``force=True``) and reconnect, mirroring ``call_peer``'s
   401 retry. A non-auth handshake rejection must propagate.

Sync tests driven via ``asyncio.run`` (the dist carries no pytest-asyncio
config), matching ``test_adapter_giveup.py``.
"""

from __future__ import annotations

import asyncio
import ssl

import pytest
import websockets
from websockets.exceptions import InvalidStatus

from awm import gatewayclient as gc


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeConn:
    """Stand-in for the real ``connect`` return: awaitable (``await connect()``
    -> the connection), async context manager (``async with connect()``), AND
    async iterator (``async for frame in ws``) — the three shapes the two call
    sites use. ``subscribe`` does ``async with connect(...)``; ``subscribe_peer``
    does ``conn = await connect(...)`` then ``async with conn``. One object
    serves both, exactly as ``websockets``'s ``connect`` does."""

    def __init__(self, frames):
        self._frames = iter(list(frames))

    def __await__(self):
        async def _self():
            return self
        return _self().__await__()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self._frames)
        except StopIteration:
            raise StopAsyncIteration


class _FakeResponse:
    def __init__(self, status_code):
        self.status_code = status_code


def _invalid_status(code):
    return InvalidStatus(_FakeResponse(code))


async def _collect(agen):
    out = []
    async for item in agen:
        out.append(item)
    return out


# A mixed frame sequence exercising every decode branch.
FRAMES = [
    '{"a": 1, "b": [2, 3]}',   # JSON object -> dict
    '[10, 20]',                # JSON array  -> list
    'not json at all',         # non-JSON text -> raw string
    b'\x00\x01binary',         # binary -> skipped entirely
    '"quoted"',                # JSON string -> "quoted"
]
EXPECTED = [{"a": 1, "b": [2, 3]}, [10, 20], "not json at all", "quoted"]


# ---------------------------------------------------------------------------
# Decode parity
# ---------------------------------------------------------------------------


def test_decode_parity_with_local_subscribe(monkeypatch):
    """``subscribe_peer`` yields exactly what ``subscribe`` yields for the same
    frames — the whole point (a consumer can't tell peer from local)."""

    # Local subscribe: patch websockets.connect to hand back the frames.
    def local_connect(url, **kwargs):
        assert "ssl" not in kwargs  # local subscribe is ws://, never TLS
        return FakeConn(FRAMES)

    monkeypatch.setattr(websockets, "connect", local_connect)
    local_out = asyncio.run(_collect(gc.subscribe("social", "command")))

    # Peer subscribe: stub resolve/fetch/ssl, same frames.
    monkeypatch.setattr(gc, "resolve_peer",
                        lambda peer, **k: {"edge_url": "https://mira:12100",
                                           "ssh_alias": "mira"})
    monkeypatch.setattr(gc, "fetch_peer_cred", lambda alias, **k: "BEARER")
    monkeypatch.setattr(gc, "_peer_ca", lambda: "ca.pem")
    monkeypatch.setattr(ssl, "create_default_context", lambda **k: "SSLCTX")

    def peer_connect(url, **kwargs):
        assert url == "wss://mira:12100/svc/social/emit/command"
        assert kwargs["ssl"] == "SSLCTX"
        assert ("Authorization", "Bearer BEARER") in kwargs["additional_headers"]
        return FakeConn(FRAMES)

    monkeypatch.setattr(websockets, "connect", peer_connect)
    peer_out = asyncio.run(_collect(gc.subscribe_peer("mira", "social", "command")))

    assert peer_out == EXPECTED
    assert peer_out == local_out


def test_as_header_forwarded(monkeypatch):
    """``as_`` rides an ``X-Awm-As`` connect header (like the local subscribe)."""
    monkeypatch.setattr(gc, "resolve_peer",
                        lambda peer, **k: {"edge_url": "https://mira:12100",
                                           "ssh_alias": "mira"})
    monkeypatch.setattr(gc, "fetch_peer_cred", lambda alias, **k: "BEARER")
    monkeypatch.setattr(gc, "_peer_ca", lambda: "ca.pem")
    monkeypatch.setattr(ssl, "create_default_context", lambda **k: "SSLCTX")

    seen = {}

    def peer_connect(url, **kwargs):
        seen["headers"] = kwargs["additional_headers"]
        return FakeConn([])

    monkeypatch.setattr(websockets, "connect", peer_connect)
    asyncio.run(_collect(gc.subscribe_peer("mira", "social", "command", as_="ssh")))
    assert ("X-Awm-As", "ssh") in seen["headers"]


# ---------------------------------------------------------------------------
# Rotation reconnect
# ---------------------------------------------------------------------------


def test_rotated_cred_reconnects_once(monkeypatch):
    """A 401 handshake rejection triggers exactly one force-refetch + reconnect,
    then streams from the fresh connection."""
    monkeypatch.setattr(gc, "resolve_peer",
                        lambda peer, **k: {"edge_url": "https://mira:12100",
                                           "ssh_alias": "mira"})
    monkeypatch.setattr(gc, "_peer_ca", lambda: "ca.pem")
    monkeypatch.setattr(ssl, "create_default_context", lambda **k: "SSLCTX")

    fetch_calls = []

    def fake_fetch(alias, *, force=False, **k):
        fetch_calls.append(force)
        return "fresh" if force else "stale"

    monkeypatch.setattr(gc, "fetch_peer_cred", fake_fetch)

    connect_bearers = []

    def flaky_connect(url, **kwargs):
        auth = dict(kwargs["additional_headers"])["Authorization"]
        connect_bearers.append(auth)
        if auth == "Bearer stale":
            raise _invalid_status(401)   # edge rejects the rotated bearer
        return FakeConn(FRAMES)

    monkeypatch.setattr(websockets, "connect", flaky_connect)
    out = asyncio.run(_collect(gc.subscribe_peer("mira", "social", "command")))

    assert out == EXPECTED                        # streamed from the fresh conn
    assert fetch_calls == [False, True]           # first cached, then forced
    assert connect_bearers == ["Bearer stale", "Bearer fresh"]  # exactly two tries


def test_non_auth_handshake_rejection_propagates(monkeypatch):
    """A non-401/403 handshake rejection is NOT a rotation — it propagates
    without a silent retry."""
    monkeypatch.setattr(gc, "resolve_peer",
                        lambda peer, **k: {"edge_url": "https://mira:12100",
                                           "ssh_alias": "mira"})
    monkeypatch.setattr(gc, "fetch_peer_cred", lambda alias, **k: "BEARER")
    monkeypatch.setattr(gc, "_peer_ca", lambda: "ca.pem")
    monkeypatch.setattr(ssl, "create_default_context", lambda **k: "SSLCTX")

    tries = []

    def bad_connect(url, **kwargs):
        tries.append(1)
        raise _invalid_status(503)   # edge unavailable, not an auth problem

    monkeypatch.setattr(websockets, "connect", bad_connect)
    with pytest.raises(InvalidStatus):
        asyncio.run(_collect(gc.subscribe_peer("mira", "social", "command")))
    assert len(tries) == 1           # no retry on a non-auth rejection


def test_second_auth_rejection_propagates(monkeypatch):
    """If the freshly-fetched cred is ALSO rejected, give up (no infinite loop)."""
    monkeypatch.setattr(gc, "resolve_peer",
                        lambda peer, **k: {"edge_url": "https://mira:12100",
                                           "ssh_alias": "mira"})
    monkeypatch.setattr(gc, "fetch_peer_cred", lambda alias, **k: "BEARER")
    monkeypatch.setattr(gc, "_peer_ca", lambda: "ca.pem")
    monkeypatch.setattr(ssl, "create_default_context", lambda **k: "SSLCTX")

    tries = []

    def always_401(url, **kwargs):
        tries.append(1)
        raise _invalid_status(403)

    monkeypatch.setattr(websockets, "connect", always_401)
    with pytest.raises(InvalidStatus):
        asyncio.run(_collect(gc.subscribe_peer("mira", "social", "command")))
    assert len(tries) == 2           # tried twice (force-refetch), then gave up


# ---------------------------------------------------------------------------
# Local-or-peer selectors — the single branch point
# ---------------------------------------------------------------------------


def test_peer_env(monkeypatch):
    monkeypatch.delenv("AWM_SOCIAL_PEER", raising=False)
    assert gc.peer_env("AWM_SOCIAL_PEER") is None       # unset -> local
    monkeypatch.setenv("AWM_SOCIAL_PEER", "   ")
    assert gc.peer_env("AWM_SOCIAL_PEER") is None       # blank -> local
    monkeypatch.setenv("AWM_SOCIAL_PEER", " mira ")
    assert gc.peer_env("AWM_SOCIAL_PEER") == "mira"     # set (trimmed) -> peer


def test_call_maybe_peer_routes(monkeypatch):
    """Falsy peer -> local call(); a peer name -> call_peer() to that peer."""
    calls = []

    async def fake_call(*a, **k):
        calls.append(("local", a, k))
        return "L"

    async def fake_call_peer(*a, **k):
        calls.append(("peer", a, k))
        return "P"

    monkeypatch.setattr(gc, "call", fake_call)
    monkeypatch.setattr(gc, "call_peer", fake_call_peer)

    async def _local():
        return await gc.call_maybe_peer(None, "social", "send", {"x": 1})

    async def _peer():
        return await gc.call_maybe_peer("mira", "social", "send", {"x": 1})

    assert asyncio.run(_local()) == "L"
    assert calls[-1][0] == "local"
    assert calls[-1][1] == ("social", "send", {"x": 1})  # no peer arg prepended

    assert asyncio.run(_peer()) == "P"
    assert calls[-1][0] == "peer"
    assert calls[-1][1] == ("mira", "social", "send", {"x": 1})  # peer first


def test_subscribe_maybe_peer_routes(monkeypatch):
    """Falsy peer -> subscribe(); a peer name -> subscribe_peer()."""
    routed = []

    async def fake_subscribe(service, topic, *, as_=None, on_connect=None):
        routed.append(("local", service, topic))
        yield {"ok": "local"}

    async def fake_subscribe_peer(peer, service, topic, *, as_=None,
                                  on_connect=None):
        routed.append(("peer", peer, service, topic))
        yield {"ok": "peer"}

    monkeypatch.setattr(gc, "subscribe", fake_subscribe)
    monkeypatch.setattr(gc, "subscribe_peer", fake_subscribe_peer)

    local = asyncio.run(_collect(gc.subscribe_maybe_peer(None, "social", "command")))
    assert local == [{"ok": "local"}]
    assert routed[-1] == ("local", "social", "command")

    peer = asyncio.run(_collect(gc.subscribe_maybe_peer("mira", "social", "command")))
    assert peer == [{"ok": "peer"}]
    assert routed[-1] == ("peer", "mira", "social", "command")
