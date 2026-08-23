"""The opt-in Origin rewrite, and the fact that it stays off by default.

``_HOP`` drops the browser's ``Host`` so httpx derives it from the upstream
URL. An upstream that only reads ``Origin`` is fine with that; an upstream that
*compares* ``Origin`` to ``Host`` is not — the pair can never match, so every
request 403s. ``rewrite_origin`` restores the comparison, and these pin that it
happens on both transports (a rewrite on HTTP alone yields a UI that loads and
then silently never streams) and nowhere else.
"""

from __future__ import annotations

import httpx
import pytest
from starlette.testclient import TestClient

from awm.httpsfront import proxy
from awm.httpsfront.auth import COOKIE_NAME

pytestmark = [pytest.mark.unit, pytest.mark.smoke]

TOKEN = "signed.session.token"
UPSTREAM = "http://127.0.0.1:12311/"
BROWSER_ORIGIN = "https://front.example:12301"


class _StubGate:
    async def authenticate(self, *, cookie=None, bearer=None):
        return (cookie == TOKEN), None

    async def session_ttl_seconds(self) -> float:
        return 3600.0

    async def verify_password(self, password: str):
        return None


def _app(*, rewrite_origin: bool):
    app = proxy.build_app(UPSTREAM, "/nonexistent/ca.pem", landing=False,
                          rewrite_origin=rewrite_origin)
    app.state.gate = _StubGate()
    return app


def _client(app) -> TestClient:
    c = TestClient(app, base_url=BROWSER_ORIGIN, follow_redirects=False)
    c.cookies.set(COOKIE_NAME, TOKEN, domain="front.example")
    return c


async def _body():
    yield b"ok"


def _seen_http(app, headers) -> dict[str, str]:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update({k.lower(): v for k, v in request.headers.items()})
        return httpx.Response(200, content=_body())

    with _client(app) as c:
        c.app.state.client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler))
        assert c.get("/api/session", headers=headers).status_code == 200
    return seen


def test_http_origin_is_rewritten_to_match_the_host_httpx_derives():
    seen = _seen_http(_app(rewrite_origin=True), {"origin": BROWSER_ORIGIN})
    # The upstream's own check is Origin == Host; both sides must read the same.
    assert seen["origin"] == "http://127.0.0.1:12311"
    assert seen["host"] == "127.0.0.1:12311"


def test_a_request_without_an_origin_does_not_gain_one():
    """Minting one would turn a same-origin navigation into a cross-origin
    request at the upstream, which is a stricter check, not a looser one."""
    seen = _seen_http(_app(rewrite_origin=True), {})
    assert "origin" not in seen


def test_origin_is_forwarded_verbatim_by_default():
    """claude-science allowlists the browser origin on its WS upgrades and
    breaks if this ever starts defaulting on."""
    seen = _seen_http(_app(rewrite_origin=False), {"origin": BROWSER_ORIGIN})
    assert seen["origin"] == BROWSER_ORIGIN


# ---------------------------------------------------------------------------
# The WebSocket path builds its forwarded headers separately from the HTTP one
# ---------------------------------------------------------------------------

def _ws_headers(monkeypatch, app, headers) -> dict[str, str]:
    """Capture what _ws_proxy would hand websockets.connect, then abort."""
    captured: dict[str, str] = {}

    async def _fake_connect(url, *, additional_headers=None, **kw):
        captured.update({k.lower(): v for k, v in (additional_headers or {}).items()})
        raise RuntimeError("upstream refused")  # closes the handshake cleanly

    monkeypatch.setattr(proxy.websockets, "connect", _fake_connect)
    # TestClient does not apply its cookie jar to a WS handshake, so the edge
    # session goes on the request directly.
    headers = {**headers, "cookie": f"{COOKIE_NAME}={TOKEN}"}
    with _client(app) as c:
        try:
            with c.websocket_connect("/api/stream", headers=headers):
                pass
        except Exception:  # noqa: BLE001 — the reject is the expected path
            pass
    return captured


def test_ws_origin_is_rewritten_too(monkeypatch):
    seen = _ws_headers(monkeypatch, _app(rewrite_origin=True),
                       {"origin": BROWSER_ORIGIN})
    assert seen["origin"] == "http://127.0.0.1:12311"


def test_ws_origin_is_verbatim_by_default(monkeypatch):
    seen = _ws_headers(monkeypatch, _app(rewrite_origin=False),
                       {"origin": BROWSER_ORIGIN})
    assert seen["origin"] == BROWSER_ORIGIN
