"""Tests for the direct-session lease client (federation slot arbiter).

``acquire_lease`` / ``acquire_lease_peer`` open a direct-session WS whose OPEN
socket IS a lease; the first frame is the owning service's grant/deny, and the
holder later reports a verdict on the same socket. The protocol logic lives in
:class:`Lease` and ``_read_grant`` — asserted here against a fake ws (no gateway,
no sockets, no real ssh). Routing of ``acquire_lease_maybe_peer`` is checked by
monkeypatching the two concrete openers.

Sync tests driven via ``asyncio.run`` (the dist carries no pytest-asyncio
config), matching ``test_subscribe_peer.py``.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from awm import gatewayclient as gc


class FakeWS:
    """Minimal stand-in for a websockets client connection."""

    def __init__(self, first_frame: str | None):
        self._first = first_frame
        self.sent: list[str] = []
        self.closed = False

    async def recv(self) -> str:
        if self._first is None:
            raise RuntimeError("no grant frame")
        return self._first

    async def send(self, raw: str) -> None:
        if self.closed:
            raise RuntimeError("send on closed ws")
        self.sent.append(raw)

    async def close(self) -> None:
        self.closed = True


# -- _read_grant ------------------------------------------------------------


def test_read_grant_parses_granted():
    ws = FakeWS(json.dumps({"lease": "granted"}))
    lease = asyncio.run(gc._read_grant(ws))
    assert lease.granted is True
    assert lease.status == "granted"


def test_read_grant_parses_busy_and_locked():
    for status in ("busy", "locked"):
        ws = FakeWS(json.dumps({"lease": status, "reason": "held"}))
        lease = asyncio.run(gc._read_grant(ws))
        assert lease.granted is False
        assert lease.status == status
        assert lease.reason == "held"


def test_read_grant_missing_frame_raises_and_closes():
    ws = FakeWS(None)
    with pytest.raises(gc.PeerError):
        asyncio.run(gc._read_grant(ws))
    assert ws.closed is True


# -- Lease.verdict / aclose -------------------------------------------------


def test_verdict_sends_frame_then_closes():
    ws = FakeWS(json.dumps({"lease": "granted"}))
    lease = asyncio.run(gc._read_grant(ws))

    async def go():
        await lease.verdict(ok=True, reason="")
    asyncio.run(go())

    assert ws.closed is True
    assert len(ws.sent) == 1
    assert json.loads(ws.sent[0]) == {"verdict": "ok", "reason": ""}


def test_verdict_fail_carries_reason():
    ws = FakeWS(json.dumps({"lease": "granted"}))
    lease = asyncio.run(gc._read_grant(ws))
    asyncio.run(lease.verdict(ok=False, reason="master never came up"))
    assert json.loads(ws.sent[0]) == {
        "verdict": "fail", "reason": "master never came up"}


def test_verdict_is_idempotent():
    ws = FakeWS(json.dumps({"lease": "granted"}))
    lease = asyncio.run(gc._read_grant(ws))

    async def go():
        await lease.verdict(ok=True)
        await lease.verdict(ok=False)   # already closed → no-op
        await lease.aclose()            # also a no-op
    asyncio.run(go())

    assert len(ws.sent) == 1            # only the first verdict went out


def test_aclose_drops_without_verdict():
    ws = FakeWS(json.dumps({"lease": "granted"}))
    lease = asyncio.run(gc._read_grant(ws))
    asyncio.run(lease.aclose())
    assert ws.closed is True
    assert ws.sent == []               # a drop sends no verdict


def test_context_manager_closes_on_exit():
    ws = FakeWS(json.dumps({"lease": "busy", "reason": "held"}))

    async def go():
        lease = await gc._read_grant(ws)
        async with lease as held:
            assert held.granted is False
        return held
    lease = asyncio.run(go())
    assert ws.closed is True           # __aexit__ dropped the socket


# -- acquire_lease_maybe_peer routing ---------------------------------------


def test_maybe_peer_routes_local_when_selector_unset(monkeypatch):
    calls = {}

    async def _local(service, host, *, kind="lease", as_=None, node=None,
                     timeout=10.0):
        calls["where"] = ("local", service, host)
        calls["node"] = node
        return "L"

    async def _peer(peer, service, host, *, kind="lease", as_=None, node=None,
                    timeout=10.0):
        calls["where"] = ("peer", peer, service, host)
        calls["node"] = node
        return "P"

    monkeypatch.setattr(gc, "acquire_lease", _local)
    monkeypatch.setattr(gc, "acquire_lease_peer", _peer)

    out = asyncio.run(gc.acquire_lease_maybe_peer(None, "ssh", "fir"))
    assert out == "L"
    assert calls["where"] == ("local", "ssh", "fir")


def test_maybe_peer_routes_to_peer_when_selector_set(monkeypatch):
    calls = {}

    async def _local(service, host, *, kind="lease", as_=None, node=None,
                     timeout=10.0):
        calls["where"] = ("local", service, host)
        calls["node"] = node
        return "L"

    async def _peer(peer, service, host, *, kind="lease", as_=None, node=None,
                    timeout=10.0):
        calls["where"] = ("peer", peer, service, host)
        calls["node"] = node
        return "P"

    monkeypatch.setattr(gc, "acquire_lease", _local)
    monkeypatch.setattr(gc, "acquire_lease_peer", _peer)

    out = asyncio.run(gc.acquire_lease_maybe_peer("mira", "ssh", "fir"))
    assert out == "P"
    assert calls["where"] == ("peer", "mira", "ssh", "fir")


def test_maybe_peer_carries_the_requesting_node_either_way(monkeypatch):
    """The arbiter pages on someone else's behalf, so it can only name the
    requester if the requester's name travels with the lease. Pin that it does
    on both branches — a selector change must not silently drop it."""
    seen = []

    async def _local(service, host, *, kind="lease", as_=None, node=None,
                     timeout=10.0):
        seen.append(("local", node))
        return "L"

    async def _peer(peer, service, host, *, kind="lease", as_=None, node=None,
                    timeout=10.0):
        seen.append(("peer", node))
        return "P"

    monkeypatch.setattr(gc, "acquire_lease", _local)
    monkeypatch.setattr(gc, "acquire_lease_peer", _peer)

    asyncio.run(gc.acquire_lease_maybe_peer(None, "ssh", "fir", node="altair"))
    asyncio.run(gc.acquire_lease_maybe_peer("mira", "ssh", "fir", node="altair"))
    assert seen == [("local", "altair"), ("peer", "altair")]


def test_the_verdict_frame_reports_the_holder(monkeypatch):
    """The other half of the wire: a Lease that knows its node says so."""
    sent = []

    class _WS:
        async def send(self, raw):
            sent.append(json.loads(raw))

        async def close(self):
            pass

    lease = gc.Lease(_WS(), "granted", None, node="capella")
    asyncio.run(lease.verdict(ok=False, reason="master never came up"))
    assert sent == [{"verdict": "fail", "reason": "master never came up",
                     "node": "capella"}]


def test_a_lease_with_no_node_omits_the_field(monkeypatch):
    """Backwards compatible: an arbiter reading an older client's frame sees no
    `node` key rather than an empty one it would have to special-case."""
    sent = []

    class _WS:
        async def send(self, raw):
            sent.append(json.loads(raw))

        async def close(self):
            pass

    lease = gc.Lease(_WS(), "granted", None)
    asyncio.run(lease.verdict(ok=True))
    assert sent == [{"verdict": "ok", "reason": ""}]
