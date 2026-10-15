"""Signed node tokens on outbound peer calls: ``invoke_peer`` / ``call_peer``.

Every HTTP leg is faked with one ``httpx.MockTransport`` that answers as the
local gateway (peer resolve, ``auth.sign_peer_token``) and as the peer's edge.
Sync tests driven via ``asyncio.run``, matching ``test_fetch_peer_cred.py``.

What is pinned here:

* the token goes first, with ``aud`` = the peer's node label, cached until
  shortly before it expires;
* a domestic peer whose edge refuses the token (or a node that cannot sign one)
  gets the legacy ssh bearer exactly once;
* a foreign peer never does.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from awm import gatewayclient as gc

# Captured at import, before conftest's per-test fixture stubs the signers out.
REAL_PEER_TOKEN, REAL_PEER_TOKEN_SYNC = gc.peer_token, gc.peer_token_sync

EDGE = "https://hub.example:12100"


class Net:
    def __init__(self, relation="domestic"):
        self.relation = relation
        self.sign_status = 200
        self.expires_in = 300
        self.refuse = lambda auth: False   # does the edge refuse this credential?
        self.edge_status = 200
        self.minted = []                   # aud of every sign_peer_token call
        self.edge_calls = []               # (path, Authorization, json body)

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.startswith("/peers/"):
            return httpx.Response(200, json={"peer": {
                "edge_url": EDGE, "ssh_alias": "hub-ssh", "relation": self.relation}})
        if path == "/svc/auth/fn/sign_peer_token":
            assert "X-Awm-As" not in request.headers, "signing is an operator call"
            aud = json.loads(request.content)["aud"]
            self.minted.append(aud)
            if self.sign_status != 200:
                return httpx.Response(self.sign_status, text="no node key")
            return httpx.Response(200, json={
                "token": f"awmpt1.t{len(self.minted)}.sig", "iss": "me", "aud": aud,
                "expires_in": self.expires_in})
        auth = request.headers.get("Authorization", "")
        body = json.loads(request.content) if request.content else None
        self.edge_calls.append((path, auth, body))
        if self.refuse(auth):
            return httpx.Response(401, text="unauthenticated")
        if self.edge_status != 200:
            return httpx.Response(self.edge_status, json={"detail": "Unknown tool: x"})
        if path == "/invoke":
            return httpx.Response(200, json={"result": json.dumps({"hits": [1, 2]})})
        if "/session/" in path:
            return httpx.Response(200, json={"ws_path": "/svc/ssh/session/abc"})
        return httpx.Response(200, json={"ok": True})


@pytest.fixture()
def net(monkeypatch):
    n = Net()
    transport = httpx.MockTransport(n.handler)
    real_async, real_sync = httpx.AsyncClient, httpx.Client
    monkeypatch.setattr(httpx, "AsyncClient",
                        lambda **kw: real_async(transport=transport))
    monkeypatch.setattr(httpx, "Client", lambda **kw: real_sync(transport=transport))
    monkeypatch.setattr(gc, "peer_token", REAL_PEER_TOKEN)
    monkeypatch.setattr(gc, "peer_token_sync", REAL_PEER_TOKEN_SYNC)
    monkeypatch.setenv("AWM_HUB_URL", "http://gateway.test")
    monkeypatch.setattr(gc, "_peer_ca", lambda: "/nonexistent/ca.pem")
    gc._peer_addr_cache.clear()
    gc._peer_token_cache.clear()
    gc._peer_token_refused.clear()
    n.legacy_fetches = []

    def fake_cred(alias, *, force=False, timeout=15.0):
        n.legacy_fetches.append((alias, force))
        return "legacy-bearer"

    monkeypatch.setattr(gc, "fetch_peer_cred", fake_cred)
    yield n
    gc._peer_addr_cache.clear()
    gc._peer_token_cache.clear()


def _auths(net):
    return [a for _, a, _ in net.edge_calls]


# ---------------------------------------------------------------------------
# The token goes first
# ---------------------------------------------------------------------------


def test_invoke_peer_posts_to_the_edge_with_a_signed_token(net):
    out = asyncio.run(gc.invoke_peer("Hub", "kb_search", {"q": "x"}))
    assert out == {"hits": [1, 2]}
    assert net.edge_calls == [("/invoke", "Bearer awmpt1.t1.sig",
                               {"name": "kb_search", "args": {"q": "x"}})]
    assert net.minted == ["hub"], "aud is the peer's node label"
    assert net.legacy_fetches == []


def test_invoke_peer_sync_matches(net):
    assert gc.invoke_peer_sync("hub", "kb", {"verb": "get"}) == {"hits": [1, 2]}
    assert net.edge_calls[0][1] == "Bearer awmpt1.t1.sig"
    assert net.edge_calls[0][2] == {"name": "kb", "args": {"verb": "get"}}


def test_args_default_to_empty(net):
    asyncio.run(gc.invoke_peer("hub", "kb_stats"))
    assert net.edge_calls[0][2] == {"name": "kb_stats", "args": {}}


def test_a_non_json_result_comes_back_as_text(net, monkeypatch):
    original = net.handler

    def handler(request):
        if request.url.path == "/invoke":
            return httpx.Response(200, json={"result": "plain words"})
        return original(request)

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _REAL_ASYNC(
        transport=httpx.MockTransport(handler)))
    assert asyncio.run(gc.invoke_peer("hub", "x")) == "plain words"


_REAL_ASYNC = httpx.AsyncClient


def test_a_token_is_reused_until_shortly_before_it_expires(net):
    asyncio.run(gc.invoke_peer("hub", "a"))
    asyncio.run(gc.invoke_peer("hub", "b"))
    gc.invoke_peer_sync("hub", "c")
    assert net.minted == ["hub"]
    assert set(_auths(net)) == {"Bearer awmpt1.t1.sig"}


def test_a_token_about_to_expire_is_replaced(net):
    net.expires_in = 20          # inside the refresh margin: never cached usefully
    asyncio.run(gc.invoke_peer("hub", "a"))
    asyncio.run(gc.invoke_peer("hub", "b"))
    assert net.minted == ["hub", "hub"]
    assert _auths(net) == ["Bearer awmpt1.t1.sig", "Bearer awmpt1.t2.sig"]


def test_each_peer_gets_its_own_token(net):
    asyncio.run(gc.invoke_peer("hub", "a"))
    asyncio.run(gc.invoke_peer("Mira.example.org", "a"))
    assert net.minted == ["hub", "mira"]


# ---------------------------------------------------------------------------
# Domestic fallback
# ---------------------------------------------------------------------------


def test_a_domestic_peer_that_refuses_the_token_gets_the_legacy_bearer_once(net):
    net.refuse = lambda auth: auth.startswith("Bearer awmpt1.")
    net.edge_status = 200
    assert asyncio.run(gc.invoke_peer("hub", "kb_search")) == {"hits": [1, 2]}
    assert _auths(net) == ["Bearer awmpt1.t1.sig", "Bearer legacy-bearer"]
    assert net.legacy_fetches == [("hub-ssh", False)]


def test_the_legacy_bearer_is_refetched_once_on_a_401(net):
    net.refuse = lambda auth: True
    with pytest.raises(gc.GatewayCallError) as exc:
        asyncio.run(gc.invoke_peer("hub", "kb_search"))
    assert exc.value.status == 401
    assert [f[1] for f in net.legacy_fetches] == [False, True]
    assert len(net.edge_calls) == 3, "token, legacy, forced legacy: no further tries"


def test_a_domestic_node_that_cannot_sign_falls_back_without_calling_the_edge_twice(net):
    net.sign_status = 500
    net.edge_status = 200
    assert asyncio.run(gc.invoke_peer("hub", "kb_search")) == {"hits": [1, 2]}
    assert _auths(net) == ["Bearer legacy-bearer"]


def test_a_peer_that_refused_the_token_is_called_with_the_legacy_bearer_for_a_while(net):
    net.refuse = lambda auth: auth.startswith("Bearer awmpt1.")
    asyncio.run(gc.invoke_peer("hub", "a"))
    asyncio.run(gc.invoke_peer("hub", "b"))
    gc.invoke_peer_sync("hub", "c")
    assert _auths(net) == ["Bearer awmpt1.t1.sig", "Bearer legacy-bearer",
                           "Bearer legacy-bearer", "Bearer legacy-bearer"]
    assert net.minted == ["hub"], "no signing RPC and no token request after the refusal"


def test_the_token_is_tried_again_once_the_refusal_memory_expires(net):
    net.refuse = lambda auth: auth.startswith("Bearer awmpt1.")
    asyncio.run(gc.invoke_peer("hub", "a"))
    net.refuse = lambda auth: False
    for aud in list(gc._peer_token_refused):
        gc._peer_token_refused[aud] = 0.0           # the window has passed
    asyncio.run(gc.invoke_peer("hub", "b"))
    assert _auths(net)[-1] == "Bearer awmpt1.t2.sig"


def test_a_403_is_not_a_reason_to_fall_back(net):
    net.edge_status = 403
    with pytest.raises(gc.GatewayCallError) as exc:
        asyncio.run(gc.invoke_peer("hub", "kb_add"))
    assert exc.value.status == 403
    assert net.legacy_fetches == [] and len(net.edge_calls) == 1
    assert gc._peer_token_refused == {}


def test_a_refused_token_logs_the_audience_it_named(net, caplog):
    import logging

    net.refuse = lambda auth: auth.startswith("Bearer awmpt1.")
    with caplog.at_level(logging.WARNING, logger="awm.gatewayclient"):
        asyncio.run(gc.invoke_peer("Hub.example.org", "a"))
    assert "aud='hub'" in caplog.text


def test_a_foreign_peer_is_retried_with_a_token_every_time(net):
    net.relation = "foreign"
    net.refuse = lambda auth: True
    for _ in range(2):
        with pytest.raises(gc.GatewayCallError):
            asyncio.run(gc.invoke_peer("hub", "a"))
    assert len(net.edge_calls) == 2 and gc._peer_token_refused == {}


def test_an_entry_whose_relation_is_unknown_fails_closed(net):
    net.relation = None
    net.refuse = lambda auth: True
    with pytest.raises(gc.GatewayCallError):
        asyncio.run(gc.invoke_peer("hub", "a"))
    assert net.legacy_fetches == []


def test_a_json_looking_scalar_result_stays_a_string(net, monkeypatch):
    original = net.handler
    for text in ("42", "true", "null", '"quoted"'):
        def handler(request, text=text):
            if request.url.path == "/invoke":
                return httpx.Response(200, json={"result": text})
            return original(request)

        monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _REAL_ASYNC(
            transport=httpx.MockTransport(handler)))
        assert asyncio.run(gc.invoke_peer("hub", "x")) == text


def test_call_peer_uses_the_token_then_falls_back_when_domestic(net):
    net.edge_status = 200
    assert asyncio.run(gc.call_peer("hub", "kb", "get", {"id": 1})) == {"ok": True}
    assert net.edge_calls[0] == ("/svc/kb/fn/get", "Bearer awmpt1.t1.sig", {"id": 1})

    gc._peer_token_cache.clear()
    net.edge_calls.clear()
    net.refuse = lambda auth: auth.startswith("Bearer awmpt1.")
    assert gc.call_peer_sync("hub", "kb", "get", {"id": 1}) == {"ok": True}
    assert _auths(net) == ["Bearer awmpt1.t2.sig", "Bearer legacy-bearer"]


# ---------------------------------------------------------------------------
# Foreign: never the legacy bearer
# ---------------------------------------------------------------------------


def test_a_foreign_peer_that_refuses_the_token_is_not_offered_the_legacy_bearer(net):
    net.relation = "foreign"
    net.refuse = lambda auth: True
    with pytest.raises(gc.GatewayCallError) as exc:
        asyncio.run(gc.invoke_peer("hub", "kb_search"))
    assert exc.value.status == 401
    assert net.legacy_fetches == []
    assert _auths(net) == ["Bearer awmpt1.t1.sig"]


def test_a_foreign_peer_that_cannot_be_signed_for_is_a_peer_error(net):
    net.relation = "foreign"
    net.sign_status = 500
    with pytest.raises(gc.PeerError):
        asyncio.run(gc.invoke_peer("hub", "kb_search"))
    with pytest.raises(gc.PeerError):
        gc.invoke_peer_sync("hub", "kb_search")
    assert net.legacy_fetches == [] and net.edge_calls == []


def test_a_foreign_peers_refusal_of_a_verb_is_a_gateway_call_error(net):
    net.relation = "foreign"
    net.edge_status = 404
    with pytest.raises(gc.GatewayCallError) as exc:
        asyncio.run(gc.invoke_peer("hub", "kb_add"))
    assert exc.value.status == 404
    assert net.legacy_fetches == []


def test_call_peer_never_falls_back_for_a_foreign_peer(net):
    net.relation = "foreign"
    net.refuse = lambda auth: True
    with pytest.raises(gc.GatewayCallError):
        gc.call_peer_sync("hub", "kb", "get", {})
    assert net.legacy_fetches == []


def test_an_unknown_peer_is_a_peer_error(net, monkeypatch):
    original = net.handler

    def handler(request):
        if request.url.path.startswith("/peers/"):
            return httpx.Response(404)
        return original(request)

    monkeypatch.setattr(httpx, "Client", lambda **kw: _REAL_SYNC(
        transport=httpx.MockTransport(handler)))
    with pytest.raises(gc.PeerError, match="unknown peer"):
        gc.invoke_peer_sync("nobody", "x")


_REAL_SYNC = httpx.Client


# ---------------------------------------------------------------------------
# File fetch rides the same path
# ---------------------------------------------------------------------------


def test_a_file_fetch_sends_the_token_and_lands_the_bytes(net, tmp_path):
    got = gc.fetch_peer_file_sync("hub", "/files/tmp/a.txt", dest_dir=str(tmp_path))
    assert json.loads(open(got, "rb").read()) == {"ok": True}
    assert _auths(net) == ["Bearer awmpt1.t1.sig"]
    assert net.legacy_fetches == []


def test_a_file_fetch_from_a_domestic_peer_falls_back_to_the_legacy_bearer(net, tmp_path):
    net.refuse = lambda auth: auth.startswith("Bearer awmpt1.")
    got = gc.fetch_peer_file_sync("hub", "/files/tmp/a.txt", dest_dir=str(tmp_path))
    assert open(got, "rb").read()
    assert _auths(net) == ["Bearer awmpt1.t1.sig", "Bearer legacy-bearer"]


def test_a_file_fetch_from_a_foreign_peer_fails_cleanly_without_the_legacy_bearer(
        net, tmp_path):
    net.relation = "foreign"
    net.edge_status = 404          # a foreign edge serves only /tools and /invoke
    with pytest.raises(gc.PeerError, match="no file"):
        gc.fetch_peer_file_sync("hub", "/files/tmp/a.txt", dest_dir=str(tmp_path))
    net.refuse = lambda auth: True
    with pytest.raises(gc.PeerError, match="unauthorized"):
        gc.fetch_peer_file_sync("hub", "/files/tmp/a.txt", dest_dir=str(tmp_path))
    assert net.legacy_fetches == []
    assert list(tmp_path.iterdir()) == [], "a refused fetch leaves no file"


def test_the_async_file_fetch_uses_the_token_too(net, tmp_path):
    got = asyncio.run(gc.fetch_peer_file("hub", "/files/tmp/b.txt", dest_dir=str(tmp_path)))
    assert got.endswith("b.txt")
    assert _auths(net) == ["Bearer awmpt1.t1.sig"]


# ---------------------------------------------------------------------------
# Streams ride the same path
# ---------------------------------------------------------------------------


class _Conn:
    def __init__(self, frames=()):
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


def _handshake_refused(code):
    from websockets.exceptions import InvalidStatus
    return InvalidStatus(SimpleResponse(code))


class SimpleResponse:
    def __init__(self, status_code):
        self.status_code = status_code


@pytest.fixture()
def ws(net, monkeypatch):
    """``websockets.connect`` replaced; ``ws.refuse(bearer) -> status | None``."""
    import ssl

    import websockets

    state = type("WS", (), {})()
    state.auths = []
    state.refuse = lambda bearer: None
    monkeypatch.setattr(ssl, "create_default_context", lambda **k: "CTX")

    def connect(url, *, additional_headers, **kw):
        auth = dict(additional_headers)["Authorization"]
        state.auths.append(auth)
        status = state.refuse(auth.removeprefix("Bearer "))
        if status:
            raise _handshake_refused(status)
        return _Conn(['{"n": 1}'])

    monkeypatch.setattr(websockets, "connect", connect)
    return state


async def _drain(agen):
    return [x async for x in agen]


def test_a_subscription_connects_with_the_token(net, ws):
    assert asyncio.run(_drain(gc.subscribe_peer("hub", "social", "command"))) == [{"n": 1}]
    assert ws.auths == ["Bearer awmpt1.t1.sig"]
    assert net.legacy_fetches == []


def test_a_domestic_subscription_falls_back_when_the_token_is_refused(net, ws):
    ws.refuse = lambda b: 401 if b.startswith("awmpt1.") else None
    assert asyncio.run(_drain(gc.subscribe_peer("hub", "social", "command"))) == [{"n": 1}]
    assert ws.auths == ["Bearer awmpt1.t1.sig", "Bearer legacy-bearer"]


def test_a_foreign_subscription_never_gets_the_legacy_bearer(net, ws):
    from websockets.exceptions import InvalidStatus

    net.relation = "foreign"
    ws.refuse = lambda b: 404          # the foreign edge has no /svc door
    with pytest.raises(InvalidStatus):
        asyncio.run(_drain(gc.subscribe_peer("hub", "social", "command")))
    ws.refuse = lambda b: 403
    with pytest.raises(InvalidStatus):
        asyncio.run(_drain(gc.subscribe_peer("hub", "social", "command")))
    assert net.legacy_fetches == []
    assert len(ws.auths) == 2 and all(a.startswith("Bearer awmpt1.") for a in ws.auths)


def test_a_foreign_subscription_without_a_signing_key_is_a_peer_error(net, ws):
    net.relation = "foreign"
    net.sign_status = 500
    with pytest.raises(gc.PeerError):
        asyncio.run(_drain(gc.subscribe_peer("hub", "social", "command")))
    assert ws.auths == [] and net.legacy_fetches == []


# ---------------------------------------------------------------------------
# The slot arbiter's lease rides the same path (ssh connects depend on it)
# ---------------------------------------------------------------------------


class _LeaseWS:
    async def recv(self):
        return json.dumps({"lease": "granted"})

    async def close(self):
        pass


@pytest.fixture()
def lease_ws(net, monkeypatch):
    import ssl

    import websockets

    state = type("LeaseWS", (), {})()
    state.auths = []
    state.refuse = lambda bearer: None
    monkeypatch.setattr(ssl, "create_default_context", lambda **k: "CTX")

    async def connect(url, *, additional_headers, **kw):
        auth = dict(additional_headers)["Authorization"]
        state.auths.append(auth)
        status = state.refuse(auth.removeprefix("Bearer "))
        if status:
            raise _handshake_refused(status)
        return _LeaseWS()

    monkeypatch.setattr(websockets, "connect", connect)
    return state


def test_a_lease_opens_with_a_token_on_both_legs(net, lease_ws):
    lease = asyncio.run(gc.acquire_lease_peer("hub", "ssh", "fir"))
    assert lease.granted
    assert _auths(net) == ["Bearer awmpt1.t1.sig"]
    assert lease_ws.auths == ["Bearer awmpt1.t1.sig"]
    assert net.legacy_fetches == []


def test_a_lease_from_an_unupgraded_domestic_peer_uses_the_legacy_bearer_on_both_legs(
        net, lease_ws):
    net.refuse = lambda auth: auth.startswith("Bearer awmpt1.")
    lease_ws.refuse = lambda b: 401 if b.startswith("awmpt1.") else None
    assert asyncio.run(gc.acquire_lease_peer("hub", "ssh", "fir")).granted
    assert _auths(net) == ["Bearer awmpt1.t1.sig", "Bearer legacy-bearer"]
    assert lease_ws.auths == ["Bearer legacy-bearer"], "the socket gets the credential that worked"


def test_a_lease_from_a_foreign_peer_never_sends_the_legacy_bearer(net, lease_ws):
    net.relation = "foreign"
    net.refuse = lambda auth: True
    with pytest.raises(gc.GatewayCallError):
        asyncio.run(gc.acquire_lease_peer("hub", "ssh", "fir"))
    assert net.legacy_fetches == [] and lease_ws.auths == []
    net.refuse = lambda auth: False
    lease_ws.refuse = lambda b: 401
    from websockets.exceptions import InvalidStatus
    with pytest.raises(InvalidStatus):
        asyncio.run(gc.acquire_lease_peer("hub", "ssh", "fir"))
    assert net.legacy_fetches == [] and len(lease_ws.auths) == 1, "no retry for a foreign peer"


def test_a_lease_with_no_signing_key_still_works_domestically(net, lease_ws):
    net.sign_status = 500
    assert asyncio.run(gc.acquire_lease_peer("hub", "ssh", "fir")).granted
    assert _auths(net) == ["Bearer legacy-bearer"]
    assert lease_ws.auths == ["Bearer legacy-bearer"]


# ---------------------------------------------------------------------------
# A slow local gateway must not stall the caller's event loop
# ---------------------------------------------------------------------------


@pytest.fixture()
def slow_resolve(net, monkeypatch):
    import time

    real = gc.resolve_peer

    def slow(name, **kw):
        time.sleep(0.4)
        return real(name, **kw)

    monkeypatch.setattr(gc, "resolve_peer", slow)


async def _ticks_while(coro):
    """Run ``coro`` and count 20 ms loop ticks that fired meanwhile."""
    ticks = 0

    async def ticker():
        nonlocal ticks
        while True:
            await asyncio.sleep(0.02)
            ticks += 1

    task = asyncio.ensure_future(ticker())
    try:
        await coro
    finally:
        task.cancel()
    return ticks


@pytest.mark.parametrize("call", [
    lambda: gc.invoke_peer("hub", "kb_search"),
    lambda: gc.call_peer("hub", "kb", "get", {}),
    lambda: gc.acquire_lease_peer("hub", "ssh", "fir"),
])
def test_async_peer_calls_resolve_without_blocking_the_loop(slow_resolve, net, call,
                                                            monkeypatch):
    import ssl

    import websockets

    async def connect(*a, **k):
        return _LeaseWS()

    monkeypatch.setattr(ssl, "create_default_context", lambda **k: "CTX")
    monkeypatch.setattr(websockets, "connect", connect)
    ticks = asyncio.run(_ticks_while(call()))
    assert ticks >= 10, "the loop kept running while the resolve was in flight"


def test_a_subscription_resolves_without_blocking_the_loop(slow_resolve, ws):
    ticks = asyncio.run(_ticks_while(_drain(gc.subscribe_peer("hub", "social", "t"))))
    assert ticks >= 10


def test_wait_for_can_cancel_a_call_stuck_in_the_resolve(slow_resolve):
    async def run():
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(gc.invoke_peer("hub", "x"), 0.05)

    asyncio.run(run())


def test_a_cached_resolve_costs_no_thread(net, monkeypatch):
    asyncio.run(gc.invoke_peer("hub", "a"))

    def boom(*a, **k):
        raise AssertionError("resolve_peer must not run on a cache hit")

    monkeypatch.setattr(gc, "resolve_peer", boom)
    asyncio.run(gc.invoke_peer("hub", "b"))
