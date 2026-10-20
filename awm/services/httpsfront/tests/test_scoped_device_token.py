"""Scoped device tokens at the edge: a token minted with a scope reaches only
the request shapes that scope names.

What these pin: an ``avatar-stage`` token opens the stage session with a POST
and holds its socket, and nothing else, on either profile: not another verb,
not another method, not a path with encoding or a query, not the edge's own
routes. Refused requests are a 401 that never reaches the upstream and never
falls through to a cookie riding along. The gate refuses a scoped token when a
caller gives it no shape, and refuses a scope it does not know.
"""

from __future__ import annotations

import asyncio
import time

import httpx
import pytest
from starlette.testclient import TestClient

from awm.config import tokens, usertoken
from awm.httpsfront import proxy
from awm.httpsfront.auth import COOKIE_NAME, AuthGate, Shape

pytestmark = [pytest.mark.unit, pytest.mark.smoke]

SECRET = "edge-secret"
GATEWAY = "http://127.0.0.1:7819"
OPEN = "/svc/avatar/session/stage"
SOCKET = "/svc/avatar/session/Ab3_-9xYz0Q"


@pytest.fixture(autouse=True)
def node(monkeypatch, peer_book):
    monkeypatch.setenv("AWM_NODE_NAME", "capella")
    peer_book({})


def _device(scope=usertoken.AVATAR_STAGE, sub="tony"):
    token_id, secret, token = usertoken.mint()
    entry = {"id": token_id, "sub": sub, "sha256": usertoken.digest(secret)}
    if scope:
        entry["scope"] = scope
    return token, entry


def _gate(devices) -> AuthGate:
    gate = AuthGate()
    gate._material = {"secret": SECRET, "peer_credentials": [], "peers": [],
                      "session_ttl_seconds": 3600.0, "max_session_seconds": 0,
                      "device_tokens": list(devices)}
    gate._fetched_at = time.monotonic()
    return gate


def _shape(method="POST", path=OPEN, raw=None, query=b"", websocket=False):
    return Shape(method, path, path.encode() if raw is None else raw, query, websocket)


# -- the shapes -----------------------------------------------------------------

ADMITTED = [_shape(), _shape("GET", SOCKET, websocket=True)]

REFUSED = [
    _shape("GET"),
    _shape("PUT"),
    _shape("GET", OPEN, websocket=True),
    _shape("POST", SOCKET),
    _shape("GET", SOCKET),
    _shape("POST", "/svc/avatar/session/other"),
    _shape("POST", "/svc/avatar/fn/devices"),
    _shape("POST", "/svc/notes/fn/list"),
    _shape("POST", "/invoke"),
    _shape("POST", OPEN + "/"),
    _shape("POST", OPEN, query=b"x=1"),
    _shape("POST", OPEN, raw=b"/svc/avatar/session/%73tage"),
    _shape("GET", "/svc/avatar/session/short", websocket=True),
    _shape("GET", "/svc/avatar/session/" + "a" * 65, websocket=True),
    _shape("GET", SOCKET + "/x", websocket=True),
    _shape("GET", "/svc/avatar/emit/live", websocket=True),
    _shape("GET", "/svc/drawio/emit/abcdefghijk", websocket=True),
    _shape("GET", SOCKET, query=b"a=b", websocket=True),
]


@pytest.mark.parametrize("shape", ADMITTED)
def test_the_stage_scope_admits_the_session_shapes(shape):
    assert usertoken.scope_admits(usertoken.AVATAR_STAGE, **shape._asdict())


@pytest.mark.parametrize("shape", REFUSED)
def test_the_stage_scope_refuses_every_other_shape(shape):
    assert not usertoken.scope_admits(usertoken.AVATAR_STAGE, **shape._asdict())


def test_an_unknown_scope_admits_nothing():
    for shape in ADMITTED:
        assert not usertoken.scope_admits("everything", **shape._asdict())


# -- the gate -------------------------------------------------------------------

def _auth(gate, bearer, shape=None, cookie=None):
    return asyncio.run(gate.authenticate(cookie=cookie, bearer=bearer, shape=shape))


@pytest.mark.parametrize("shape", ADMITTED)
def test_a_scoped_token_is_its_user_on_its_shapes(shape):
    token, entry = _device()
    assert _auth(_gate([entry]), token, shape) == (True, None, "tony")


@pytest.mark.parametrize("shape", REFUSED + [None])
def test_a_scoped_token_is_no_one_anywhere_else(shape):
    token, entry = _device()
    cookie = tokens.mint(SECRET, sub="tony", ttl=3600)
    assert _auth(_gate([entry]), token, shape) == (False, None, None)
    assert _auth(_gate([entry]), token, shape, cookie) == (False, None, None)


def test_a_token_with_an_unknown_scope_is_refused_on_every_shape():
    token, entry = _device(scope="everything")
    for shape in ADMITTED + [None]:
        assert _auth(_gate([entry]), token, shape) == (False, None, None)


def test_an_unscoped_token_still_needs_no_shape():
    token, entry = _device(scope=None)
    assert _auth(_gate([entry]), token) == (True, None, "tony")
    assert _auth(_gate([entry]), token, _shape("GET", "/svc/notes/fn/list")) == (
        True, None, "tony")


# -- the edge, end to end --------------------------------------------------------

async def _body():
    yield b"ok"


def _edge(gate, profile=None):
    app = proxy.build_app(GATEWAY + "/", "/dev/null", profile=profile)
    app.state.gate = gate
    sent: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append((request.method, request.url.raw_path,
                     {k.lower(): v for k, v in request.headers.items()}))
        return httpx.Response(200, content=_body())

    c = TestClient(app, base_url="https://mesh.example", follow_redirects=False)
    c.__enter__()
    c.app.state.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return c, sent


def test_the_session_open_is_forwarded_as_the_user_without_the_token():
    token, entry = _device()
    c, sent = _edge(_gate([entry]))
    try:
        r = c.post(OPEN, json={"deviceId": "phone"},
                   headers={"Authorization": f"Bearer {token}"})
    finally:
        c.__exit__(None, None, None)
    assert r.status_code == 200
    ((method, raw, fwd),) = sent
    assert (method, raw) == ("POST", OPEN.encode())
    assert fwd["x-awm-as"] == "user:tony" and "authorization" not in fwd


@pytest.mark.parametrize("method,path", [
    ("GET", OPEN), ("POST", "/svc/avatar/fn/devices"), ("POST", "/invoke"),
    ("GET", "/svc/notes/fn/list"), ("GET", "/tools"), ("GET", "/files/etc/hosts"),
    ("POST", OPEN + "?x=1"), ("GET", "/__auth/whoami"), ("GET", "/"),
])
def test_every_other_request_is_a_401_that_reaches_nothing(method, path):
    token, entry = _device()
    c, sent = _edge(_gate([entry]))
    try:
        r = c.request(method, path, headers={"Authorization": f"Bearer {token}",
                                             "Accept": "application/json"})
    finally:
        c.__exit__(None, None, None)
    assert r.status_code == 401
    assert sent == []


def test_on_the_public_profile_the_open_is_still_not_on_the_door():
    """The public profile's allow-list stays the gate there: a scope widens
    nothing."""
    token, entry = _device()
    c, sent = _edge(_gate([entry]), profile=proxy.PUBLIC_PROFILE)
    try:
        r = c.post(OPEN, headers={"Authorization": f"Bearer {token}"})
    finally:
        c.__exit__(None, None, None)
    assert r.status_code == 404
    assert sent == []


def _ws(monkeypatch, gate, path):
    dialled: list = []

    async def connect(url, *, additional_headers=None, **kw):
        dialled.append((url, {k.lower(): v for k, v in (additional_headers or {}).items()}))
        raise RuntimeError("upstream refused")

    monkeypatch.setattr(proxy.websockets, "connect", connect)
    token, entry = _device()
    gate._material["device_tokens"] = [entry]
    c, _ = _edge(gate)
    try:
        try:
            with c.websocket_connect(path, headers={"Authorization": f"Bearer {token}"}):
                pass
        except Exception:  # noqa: BLE001 — the refusal is the path under test
            pass
    finally:
        c.__exit__(None, None, None)
    return dialled


def test_the_session_socket_is_dialled_as_the_user(monkeypatch):
    dialled = _ws(monkeypatch, _gate([]), SOCKET)
    ((url, hdrs),) = dialled
    assert url.endswith(SOCKET)
    assert hdrs["x-awm-as"] == "user:tony" and "authorization" not in hdrs


@pytest.mark.parametrize("path", ["/svc/avatar/emit/live", OPEN,
                                  "/svc/drawio/emit/abcdefghijk", SOCKET + "?a=b"])
def test_no_other_socket_is_dialled(monkeypatch, path):
    assert _ws(monkeypatch, _gate([]), path) == []
