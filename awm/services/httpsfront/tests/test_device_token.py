"""Device tokens at the edge: an ``awmut1.`` bearer acts as one user, or as no one.

What these pin: a live token yields its user's name as ``sub`` and is stamped
``X-Awm-As: user:<sub>`` exactly as a cookie login is; a revoked, unknown or
malformed one is a 401 that never falls through to the peer branches or to a
cookie; a token that names a machine identity is refused; peer tokens behave
as before and never gain a user identity; no cookie is set; both listeners
honour it; the token is never forwarded upstream; and the edge reports the
tokens it accepted on its next material fetch, which an older ``auth`` that
sends no ``device_tokens`` key survives.
"""

from __future__ import annotations

import asyncio
import time

import httpx
import pytest
from starlette.testclient import TestClient

from awm.config import peertoken, tokens, usertoken
from awm.httpsfront import proxy
from awm.httpsfront.auth import COOKIE_NAME, AuthGate

pytestmark = [pytest.mark.unit, pytest.mark.smoke]

ME = "altair"
SECRET = "edge-secret"
LEGACY = "legacy-peer-credential"
GATEWAY = "http://127.0.0.1:7819"


@pytest.fixture(autouse=True)
def node(monkeypatch, peer_book):
    monkeypatch.setenv("AWM_NODE_NAME", ME)
    peer_book({"mira": {"relation": "domestic"}})


def _device(sub="tony"):
    token_id, secret, token = usertoken.mint()
    return token, {"id": token_id, "sub": sub, "sha256": usertoken.digest(secret)}


@pytest.fixture()
def keys():
    private = peertoken.generate_private_key()
    return private, peertoken.public_key_of(private)


def _gate(devices=(), *, peers=(), honour_peers=True, with_key=True) -> AuthGate:
    gate = AuthGate(peers=honour_peers)
    gate._material = {
        "secret": SECRET,
        "peer_credentials": [LEGACY],
        "peers": list(peers),
        "session_ttl_seconds": 3600.0,
        "max_session_seconds": 0,
        **({"device_tokens": list(devices)} if with_key else {}),
    }
    gate._fetched_at = time.monotonic()
    return gate


def _auth(gate, bearer=None, cookie=None):
    return asyncio.run(gate.authenticate(cookie=cookie, bearer=bearer))


# -- the gate -----------------------------------------------------------------

@pytest.mark.parametrize("honour_peers", [True, False])
def test_a_live_token_is_its_user_on_either_listener(honour_peers):
    token, entry = _device()
    gate = _gate([entry], honour_peers=honour_peers)
    assert _auth(gate, token) == (True, None, "tony")


def test_a_live_token_works_on_a_node_closed_to_peers(monkeypatch):
    monkeypatch.setenv("AWM_EDGE_PEERS", "0")
    token, entry = _device()
    assert _auth(_gate([entry]), token) == (True, None, "tony")


def _bad(kind, entry, token):
    token_id, secret = usertoken.parse(token)
    if kind == "revoked":          # auth leaves it out of the material
        return token, []
    if kind == "unknown":
        return usertoken.mint()[2], [entry]
    if kind == "wrong secret":
        return f"{usertoken.PREFIX}{token_id}.{usertoken.mint()[1]}", [entry]
    if kind == "malformed":
        return f"{usertoken.PREFIX}{token_id}.short", [entry]
    return usertoken.PREFIX, [entry]  # bare prefix


KINDS = ["revoked", "unknown", "wrong secret", "malformed", "bare prefix"]


@pytest.mark.parametrize("kind", KINDS)
def test_a_bad_token_is_refused(kind):
    token, entry = _device()
    bearer, devices = _bad(kind, entry, token)
    assert _auth(_gate(devices), bearer) == (False, None, None)


@pytest.mark.parametrize("kind", KINDS)
def test_a_bad_token_never_falls_through_to_a_peer_or_a_cookie(kind):
    """Even when the very same string is on file as a legacy bearer, and even
    with a valid session cookie riding along."""
    token, entry = _device()
    bearer, devices = _bad(kind, entry, token)
    gate = _gate(devices)
    gate._material["peer_credentials"] = [bearer, LEGACY]
    cookie = tokens.mint(SECRET, sub="tony", ttl=3600)
    assert _auth(gate, bearer) == (False, None, None)
    assert _auth(gate, bearer, cookie) == (False, None, None)


def test_a_live_token_is_never_read_as_a_legacy_bearer():
    token, entry = _device()
    gate = _gate([entry])
    gate._material["peer_credentials"] = [token]
    assert _auth(gate, token) == (True, None, "tony")


@pytest.mark.parametrize("sub", ["operator", "peer", "peer:mira", ""])
def test_a_token_naming_a_machine_is_refused(sub):
    token, entry = _device(sub)
    assert _auth(_gate([entry]), token) == (False, None, None)


def test_peer_tokens_are_unchanged_and_never_become_a_user(keys):
    private, public = keys
    _, entry = _device()
    gate = _gate([entry], peers=[{"name": "mira", "public_key": public,
                                  "relation": "domestic"}])
    node = peertoken.sign(private, iss="mira", aud=ME)
    assert _auth(gate, node) == (True, None, "peer:mira")
    assert _auth(gate, LEGACY) == (True, None, "peer")


def test_material_from_an_older_auth_without_the_key_still_works(keys):
    private, public = keys
    gate = _gate(with_key=False, peers=[{"name": "mira", "public_key": public,
                                         "relation": "domestic"}])
    token, _ = _device()
    assert _auth(gate, token) == (False, None, None)
    assert _auth(gate, LEGACY)[2] == "peer"
    assert _auth(gate, peertoken.sign(private, iss="mira", aud=ME))[2] == "peer:mira"
    assert _auth(gate, cookie=tokens.mint(SECRET, sub="tony", ttl=3600))[2] == "tony"


# -- usage reporting ----------------------------------------------------------

def test_the_edge_reports_use_on_its_next_fetch_and_then_forgets_it(monkeypatch):
    token, entry = _device()
    gate = _gate([entry])
    assert _auth(gate, token)[0]
    assert set(gate._device_used) == {entry["id"]}

    sent: list = []

    async def call(service, fn, args=None, **kw):
        sent.append((service, fn, args, kw))
        return dict(gate._material)

    from awm import gatewayclient
    monkeypatch.setattr(gatewayclient, "call", call)
    gate._fetched_at = 0.0
    asyncio.run(gate._material_fresh())
    (service, fn, args, kw), = sent
    assert (service, fn) == ("auth", "edge_material") and "as_" not in kw
    assert set(args["device_used"]) == {entry["id"]}
    assert gate._device_used == {}
    gate._fetched_at = 0.0
    asyncio.run(gate._material_fresh())
    assert sent[1][2] == {}


def test_a_failed_fetch_keeps_the_report_for_the_next_one(monkeypatch):
    token, entry = _device()
    gate = _gate([entry])
    _auth(gate, token)

    async def call(*a, **kw):
        raise RuntimeError("auth down")

    from awm import gatewayclient
    monkeypatch.setattr(gatewayclient, "call", call)
    gate._fetched_at = 0.0
    assert asyncio.run(gate._material_fresh()) is gate._material
    assert set(gate._device_used) == {entry["id"]}


# -- the edge, end to end ------------------------------------------------------

async def _body():
    yield b"ok"


def _edge(gate, profile=None):
    app = proxy.build_app(GATEWAY + "/", "/dev/null", profile=profile)
    app.state.gate = gate
    sent: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append({k.lower(): v for k, v in request.headers.items()})
        return httpx.Response(200, content=_body())

    c = TestClient(app, base_url="https://mesh.example", follow_redirects=False)
    c.__enter__()
    c.app.state.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return c, sent


def test_a_device_request_is_stamped_as_the_user_with_no_cookie_and_no_token():
    token, entry = _device()
    c, sent = _edge(_gate([entry]))
    try:
        r = c.get("/svc/notes/fn/list", headers={"Authorization": f"Bearer {token}"})
        who = c.get("/__auth/whoami", headers={"Authorization": f"Bearer {token}"})
    finally:
        c.__exit__(None, None, None)
    assert r.status_code == 200
    assert "set-cookie" not in r.headers and "set-cookie" not in who.headers
    assert who.json() == {"user": "tony"}
    (fwd,) = sent
    assert fwd["x-awm-as"] == "user:tony"
    assert "authorization" not in fwd


@pytest.mark.parametrize("kind", KINDS)
def test_a_bad_device_request_is_a_401_that_reaches_nothing(kind):
    token, entry = _device()
    bearer, devices = _bad(kind, entry, token)
    c, sent = _edge(_gate(devices))
    try:
        c.cookies.set(COOKIE_NAME, tokens.mint(SECRET, sub="tony", ttl=3600),
                      domain="mesh.example")
        r = c.get("/svc/notes/fn/list", headers={"Authorization": f"Bearer {bearer}"})
    finally:
        c.__exit__(None, None, None)
    assert r.status_code == 401
    assert sent == []


def test_the_forwarded_identity_cannot_be_forged_alongside_a_token():
    token, entry = _device()
    c, sent = _edge(_gate([entry]))
    try:
        c.get("/svc/notes/fn/list", headers={"Authorization": f"Bearer {token}",
                                             "X-Awm-As": "peer:mira"})
    finally:
        c.__exit__(None, None, None)
    assert sent[0]["x-awm-as"] == "user:tony"


def test_a_device_token_is_not_forwarded_over_a_websocket(monkeypatch):
    token, entry = _device()
    dialled: list = []

    async def connect(url, *, additional_headers=None, **kw):
        dialled.append({k.lower(): v for k, v in (additional_headers or {}).items()})
        raise RuntimeError("upstream refused")

    monkeypatch.setattr(proxy.websockets, "connect", connect)
    c, _ = _edge(_gate([entry]))
    try:
        try:
            with c.websocket_connect("/svc/drawio/emit/t",
                                     headers={"Authorization": f"Bearer {token}"}):
                pass
        except Exception:  # noqa: BLE001 — the refusal is the path under test
            pass
    finally:
        c.__exit__(None, None, None)
    assert dialled and dialled[0]["x-awm-as"] == "user:tony"
    assert "authorization" not in dialled[0]
