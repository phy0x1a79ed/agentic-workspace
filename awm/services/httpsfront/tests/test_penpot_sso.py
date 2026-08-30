"""One sign-in: the edge hands the browser the Penpot session awm holds.

What these pin: the shell document — and only the shell document — carries the
Penpot cookie; a cookie the browser already has right is left alone; an RPC 401
is the one other moment worth re-minting on, and it re-logs-in conditionally so
a page's worth of failures costs one login; signing in and signing out both drop
the previous person's Penpot cookie; and every degradation (no credential, auth
down, a machine bearer) is silence rather than an error page.
"""

from __future__ import annotations

from contextlib import contextmanager

import httpx
import pytest
from starlette.testclient import TestClient

from awm.httpsfront import penpot, proxy
from awm.httpsfront.auth import COOKIE_NAME

pytestmark = [pytest.mark.unit, pytest.mark.smoke]

TOKEN = "signed.session.token"
GATEWAY = "http://127.0.0.1:7819"
PENPOT = "http://127.0.0.1:9001"


class _Gate:
    def __init__(self, sub="tony", token="penpot-tok-1"):
        self.sub = sub
        self.token = token
        self.calls: list[dict] = []

    async def authenticate(self, *, cookie=None, bearer=None):
        if cookie == TOKEN:
            return True, None, self.sub
        return False, None, None

    async def session_ttl_seconds(self):
        return 3600.0

    async def verify_password(self, password):
        return None

    async def penpot_session(self, username, *, stale_token=None, refresh=False):
        self.calls.append({"username": username, "stale_token": stale_token,
                           "refresh": refresh})
        return self.token


async def _body():
    yield b"ok"


def _app(gate=None, *, profile="public"):
    app = proxy.build_app(GATEWAY + "/", "/dev/null", profile=profile,
                          penpot_upstream=PENPOT)
    app.state.gate = gate or _Gate()
    return app


@contextmanager
def _client(app, handler, *, penpot_cookie=None):
    c = TestClient(app, base_url="https://nexus.example", follow_redirects=False)
    c.cookies.set(COOKIE_NAME, TOKEN, domain="nexus.example")
    if penpot_cookie:
        c.cookies.set(penpot.COOKIE_NAME, penpot_cookie, domain="nexus.example")
    with c:
        c.app.state.client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler))
        yield c


def _upstream(status=200):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, content=_body())
    return handler


def _cookie(resp, name):
    """The value a response sets for ``name``, or None. Read off the raw
    headers rather than the jar: the jar cannot tell "set to empty" from
    "not set", and the deletion case turns on exactly that."""
    for key, value in resp.headers.raw:
        if key.lower() == b"set-cookie" and value.startswith(name.encode() + b"="):
            return value.decode().split("=", 1)[1].split(";")[0]
    return None


# -- the shell ----------------------------------------------------------------


def test_the_shell_carries_the_penpot_session():
    gate = _Gate()
    with _client(_app(gate), _upstream()) as c:
        resp = c.get(penpot.SHELL)
    assert _cookie(resp, penpot.COOKIE_NAME) == "penpot-tok-1"
    assert gate.calls == [{"username": "tony", "stale_token": None,
                           "refresh": False}]


def test_the_cookie_is_httponly_and_rooted_where_penpot_puts_its_own():
    with _client(_app(), _upstream()) as c:
        resp = c.get(penpot.SHELL)
    raw = [v.decode() for k, v in resp.headers.raw
           if k.lower() == b"set-cookie" and v.startswith(b"auth-token=")][0]
    assert "Path=/" in raw and "HttpOnly" in raw and "Secure" in raw


def test_a_cookie_that_is_already_right_is_not_reset():
    with _client(_app(), _upstream(), penpot_cookie="penpot-tok-1") as c:
        resp = c.get(penpot.SHELL)
    assert _cookie(resp, penpot.COOKIE_NAME) is None


def test_a_rotation_leaves_a_stale_cookie_that_the_next_shell_load_replaces():
    """The nightly rotation ends the browser's Penpot session and leaves awm's
    intact. Recovering has to be silent, or the rotation is a nightly outage."""
    with _client(_app(), _upstream(), penpot_cookie="killed-by-rotation") as c:
        resp = c.get(penpot.SHELL)
    assert _cookie(resp, penpot.COOKIE_NAME) == "penpot-tok-1"


@pytest.mark.parametrize("inner", ["js/main.js", "images/favicon.png",
                                   "assets/by-id/abc"])
def test_assets_cost_no_round_trip(inner):
    """A page load is one document and a hundred files; an auth RPC in front of
    each of those is the thing this design is careful not to do."""
    gate = _Gate()
    with _client(_app(gate), _upstream()) as c:
        c.get(penpot.SHELL + inner)
    assert gate.calls == []


def test_the_shell_is_not_bridged_for_a_machine_bearer():
    gate = _Gate(sub="peer")
    with _client(_app(gate), _upstream()) as c:
        c.get(penpot.SHELL)
    assert gate.calls == []


# -- the RPC 401 --------------------------------------------------------------


def test_a_401_re_mints_conditionally_on_the_presented_cookie():
    gate = _Gate(token="penpot-tok-2")
    with _client(_app(gate), _upstream(401), penpot_cookie="dead") as c:
        resp = c.get(penpot.SHELL + "api/rpc/command/get-profile")
    assert _cookie(resp, penpot.COOKIE_NAME) == "penpot-tok-2"
    assert gate.calls == [{"username": "tony", "stale_token": "dead",
                           "refresh": False}]


def test_an_rpc_that_worked_is_left_alone():
    gate = _Gate()
    with _client(_app(gate), _upstream(200), penpot_cookie="live") as c:
        c.post(penpot.SHELL + "api/rpc/command/get-profile")
    assert gate.calls == []


def test_a_401_that_yields_the_same_token_sets_nothing():
    gate = _Gate(token="dead")
    with _client(_app(gate), _upstream(401), penpot_cookie="dead") as c:
        resp = c.get(penpot.SHELL + "api/rpc/command/get-profile")
    assert _cookie(resp, penpot.COOKIE_NAME) is None


# -- degradation --------------------------------------------------------------


def test_no_credential_means_penpots_own_login_screen_not_an_error():
    class _NoCred(_Gate):
        async def penpot_session(self, username, *, stale_token=None, refresh=False):
            return None

    with _client(_app(_NoCred()), _upstream()) as c:
        resp = c.get(penpot.SHELL)
    assert resp.status_code == 200
    assert _cookie(resp, penpot.COOKIE_NAME) is None


def test_a_gate_without_the_capability_still_serves_penpot():
    class _Old:
        async def authenticate(self, *, cookie=None, bearer=None):
            return (True, None, "tony") if cookie == TOKEN else (False, None, None)

        async def session_ttl_seconds(self):
            return 3600.0

        async def verify_password(self, password):
            return None

    with _client(_app(_Old()), _upstream()) as c:
        resp = c.get(penpot.SHELL)
    assert resp.status_code == 200
    assert _cookie(resp, penpot.COOKIE_NAME) is None


# -- identity changes ---------------------------------------------------------


def test_signing_out_drops_the_penpot_cookie():
    """A browser that keeps it hands the next person to sign in the previous
    person's design files — worse than the second password this replaces."""
    with _client(_app(), _upstream(), penpot_cookie="tonys-session") as c:
        resp = c.get("/__auth/logout")
    raw = [v.decode() for k, v in resp.headers.raw
           if k.lower() == b"set-cookie" and v.startswith(b"auth-token=")]
    assert raw and ("Max-Age=0" in raw[0] or 'auth-token=""' in raw[0])
