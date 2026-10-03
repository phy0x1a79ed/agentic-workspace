"""What the front must carry faithfully when the upstream is not the gateway.

awm's own services never needed any of this: they are same-origin, they set no
cookies of their own, and they never ask who the browser thinks it is talking
to. A wrapped third-party app needs all three, and each one fails silently when
it is missing — a session that simply never establishes, with nothing in a log
to say why. So each is pinned here.
"""

from __future__ import annotations

import httpx
import pytest
from starlette.responses import JSONResponse
from starlette.testclient import TestClient

from awm.httpsfront import proxy
from awm.httpsfront.auth import COOKIE_NAME

pytestmark = [pytest.mark.unit, pytest.mark.smoke]

TOKEN = "signed.session.token"


class _StubGate:
    """Accepts exactly one session token; never refreshes."""

    async def authenticate(self, *, cookie=None, bearer=None):
        return (cookie == TOKEN), None

    async def session_ttl_seconds(self) -> float:
        return 3600.0

    async def verify_password(self, password: str):
        return None


def _client(app, *, authed: bool = True) -> TestClient:
    # https base URL, not http: the session cookie is Secure, so an http client
    # would drop it and every auth assertion would pass for the wrong reason.
    c = TestClient(app, base_url="https://front.example:12201",
                   follow_redirects=False)
    if authed:
        c.cookies.set(COOKIE_NAME, TOKEN, domain="front.example")
    return c


def _mock_upstream(handler):
    """Replace the lifespan's real httpx client with one that never leaves."""
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def _body():
    """A response body the front can actually stream.

    The front sends with ``stream=True`` and relays ``aiter_raw()``; a mock
    response built with ``text=`` arrives already consumed, so the fake upstream
    has to hand back a real async stream to exercise the code under test.
    """
    yield b"ok"


# ---------------------------------------------------------------------------
# X-Forwarded-Host
# ---------------------------------------------------------------------------

def test_upstream_sees_the_host_the_browser_used():
    """Without this an app derives a loopback http:// self-origin and its
    Secure cookies never stick."""
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update({k.lower(): v for k, v in request.headers.items()})
        return httpx.Response(200, content=_body())

    app = proxy.build_app("http://127.0.0.1:12203/", "/nonexistent/ca.pem",
                          landing=False)
    app.state.gate = _StubGate()
    with _client(app) as c:
        c.app.state.client = _mock_upstream(handler)
        assert c.get("/anything").status_code == 200

    assert seen["x-forwarded-host"] == "front.example:12201"
    assert seen["x-forwarded-proto"] == "https"
    # `host` stays dropped so httpx derives it from the upstream URL.
    assert seen["host"] == "127.0.0.1:12203"


# ---------------------------------------------------------------------------
# Repeated response headers
# ---------------------------------------------------------------------------

def test_two_set_cookie_headers_both_survive():
    """An app that sets a session cookie and a CSRF cookie in one response gets
    both through, or it is broken in a way no error reports."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers=[
            ("set-cookie", "app_auth=a; Path=/; HttpOnly"),
            ("set-cookie", "app_csrf=b; Path=/"),
        ], content=_body())

    app = proxy.build_app("http://127.0.0.1:12203/", "/nonexistent/ca.pem",
                          landing=False)
    app.state.gate = _StubGate()
    with _client(app) as c:
        c.app.state.client = _mock_upstream(handler)
        r = c.get("/anything")

    cookies = r.headers.get_list("set-cookie")
    assert len(cookies) == 2, cookies
    assert any(v.startswith("app_auth=") for v in cookies)
    assert any(v.startswith("app_csrf=") for v in cookies)


# ---------------------------------------------------------------------------
# extra_routes
# ---------------------------------------------------------------------------

async def _secret(request):
    return JSONResponse({"granted": True})


def _app_with_extra():
    app = proxy.build_app(
        "http://127.0.0.1:12203/", "/nonexistent/ca.pem", landing=False,
        extra_routes=[("/_signin", ["GET"], _secret)],
    )
    app.state.gate = _StubGate()
    return app


def test_extra_route_runs_for_an_authenticated_caller():
    with _client(_app_with_extra()) as c:
        r = c.get("/_signin")
    assert r.status_code == 200
    assert r.json() == {"granted": True}


def test_extra_route_is_gated_for_an_anonymous_caller():
    """The whole point of the parameter: an added route does something
    privileged, so it must not be the one unauthenticated door on the front."""
    with _client(_app_with_extra(), authed=False) as c:
        r = c.get("/_signin", headers={"accept": "application/json"})
    assert r.status_code == 401
    assert "granted" not in r.text


def test_anonymous_browser_gets_the_login_page_not_the_route():
    with _client(_app_with_extra(), authed=False) as c:
        r = c.get("/_signin", headers={"accept": "text/html"})
    # Same deny shape as the catch-all proxy: a page, not a bare 401.
    assert r.status_code == 200
    assert "granted" not in r.text


def test_extra_route_cannot_shadow_the_edge_auth_surface():
    """Registered after /__auth/*, so a caller cannot replace login."""
    app = proxy.build_app(
        "http://127.0.0.1:12203/", "/nonexistent/ca.pem", landing=False,
        extra_routes=[("/__auth/login", ["GET", "POST"], _secret)],
    )
    app.state.gate = _StubGate()
    with _client(app) as c:
        r = c.post("/__auth/login", json={"password": "nope"})
    # The edge's own handler answered (401 from verify_password), not ours.
    assert r.status_code == 401
    assert "granted" not in r.text
