"""Tests for the direct-session lease client (federation slot arbiter).

``acquire_lease`` opens a direct-session WS whose OPEN socket IS a lease; it
sends the init as its first frame, the first frame back is the owning service's grant/deny, and the holder later reports a
verdict on the same socket. The protocol logic lives in :class:`Lease` and
``_read_grant`` — asserted here against a fake ws (no gateway, no sockets).
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
    with pytest.raises(gc.GatewayCallError):
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


# -- acquire_lease: one socket, init first ---------------------------------


class _LeaseWS(FakeWS):
    pass


def _connect_spy(monkeypatch, ws):
    import websockets
    seen = {}

    async def connect(url, **kw):
        seen["url"], seen["headers"] = url, kw.get("additional_headers")
        return ws

    monkeypatch.setattr(websockets, "connect", connect)
    return seen


def test_acquire_lease_sends_init_first_on_one_socket(monkeypatch, tmp_path):
    monkeypatch.setenv("AWM_HUB_URL", "http://127.0.0.1:7819/")
    token = tmp_path / "svc.token"
    token.write_text("tok\n")
    monkeypatch.setenv("AWM_HUB_TOKEN_FILE", str(token))
    ws = _LeaseWS(json.dumps({"lease": "granted"}))
    seen = _connect_spy(monkeypatch, ws)
    lease = asyncio.run(gc.acquire_lease("ssh", "fir", node="capella", as_="tony"))
    assert lease.granted
    assert seen["url"] == "ws://127.0.0.1:7819/hub/session/ssh/lease"
    assert ("X-Awm-Service-Token", "tok") in seen["headers"]
    assert ("X-Awm-As", "tony") in seen["headers"]
    assert json.loads(ws.sent[0]) == {"host": "fir", "node": "capella"}


def test_a_lease_on_a_peer_names_the_peer(monkeypatch):
    monkeypatch.setenv("AWM_HUB_URL", "http://127.0.0.1:7819")
    monkeypatch.delenv("AWM_HUB_TOKEN_FILE", raising=False)
    ws = _LeaseWS(json.dumps({"lease": "busy", "reason": "held"}))
    seen = _connect_spy(monkeypatch, ws)
    lease = asyncio.run(gc.acquire_lease("ssh", "fir", peer="mira"))
    assert seen["url"] == "ws://127.0.0.1:7819/hub/session/ssh/lease?peer=mira"
    assert seen["headers"] is None
    assert lease.status == "busy"
    assert json.loads(ws.sent[0]) == {"host": "fir"}


def test_a_local_peer_is_no_peer(monkeypatch):
    monkeypatch.setenv("AWM_HUB_URL", "http://127.0.0.1:7819")
    seen = _connect_spy(monkeypatch, _LeaseWS(json.dumps({"lease": "granted"})))
    asyncio.run(gc.acquire_lease("ssh", "fir", peer="local"))
    assert seen["url"].endswith("/hub/session/ssh/lease")


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
