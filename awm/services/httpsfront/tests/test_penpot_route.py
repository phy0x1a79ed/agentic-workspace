"""Penpot as a third upstream on the same listener (gateway, vault, Penpot).

What these pin: the shell is rewritten to the upstream's root and nothing
else is; a trailing slash is a redirect and never the shell; cookies,
``Authorization`` and ``Origin`` reach Penpot unmodified while hop-by-hop
headers do not — the whole reason Penpot needs this bespoke upstream rather
than the generic ``kind="url"`` mount, which strips those by design; the
WebSocket leg (Penpot's collab socket) takes the same route as HTTP; the
upstream is chosen by *path*, never by anything a caller can send; Penpot's
own ``/logout`` is never hijacked, unlike the vault's; and a Penpot that has
not finished starting reads as "try again" rather than "awm is broken".
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
    """One session, belonging to a person."""

    def __init__(self, sub: str = "tony") -> None:
        self.sub = sub

    async def authenticate(self, *, cookie=None, bearer=None):
        if cookie == TOKEN:
            return True, None, self.sub
        return False, None, None

    async def session_ttl_seconds(self) -> float:
        return 3600.0

    async def verify_password(self, password: str):
        return None


async def _body():
    yield b"ok"


def _app(*, profile="public", sub="tony", with_penpot=True):
    app = proxy.build_app(GATEWAY + "/", "/dev/null", profile=profile,
                          penpot_upstream=PENPOT if with_penpot else None)
    app.state.gate = _Gate(sub)
    return app


@contextmanager
def _client(app, handler, *, authed=True):
    """A client with the upstream mocked.

    The mock is installed *after* startup: `build_app`'s lifespan owns
    `state.client` and replaces it, so anything set beforehand is discarded and
    every assertion would pass against the real transport instead.
    """
    c = TestClient(app, base_url="https://nexus.example", follow_redirects=False)
    if authed:
        c.cookies.set(COOKIE_NAME, TOKEN, domain="nexus.example")
    with c:
        c.app.state.client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler))
        yield c


def _recorder():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["headers"] = {k.lower(): v for k, v in request.headers.items()}
        return httpx.Response(200, content=_body())
    return seen, handler


# -- which upstream, and why --------------------------------------------------

def test_the_shell_is_asked_for_at_the_upstreams_root():
    seen, handler = _recorder()
    with _client(_app(), handler) as c:
        assert c.get(penpot.SHELL).status_code == 200
    assert seen["url"] == PENPOT + "/"


@pytest.mark.parametrize("path", ["/js/main.js", "/css/main.css",
                                  "/images/favicon.png", "/api/rpc/command/x",
                                  "/assets/by-id/abc"])
def test_root_level_penpot_paths_go_to_penpot_unchanged(path):
    seen, handler = _recorder()
    with _client(_app(), handler) as c:
        assert c.get(path).status_code == 200
    assert seen["url"] == PENPOT + path


@pytest.mark.parametrize("path", ["/ui/drawio/", "/svc/drawio/fn/save"])
def test_awm_paths_still_go_to_the_gateway(path):
    seen, handler = _recorder()
    with _client(_app(), handler) as c:
        assert c.get(path).status_code == 200
    assert seen["url"].startswith(GATEWAY)


def test_without_a_penpot_upstream_nothing_changes():
    """The parameter is off by default, so every existing caller is unaffected."""
    seen, handler = _recorder()
    app = _app(with_penpot=False, profile=None)
    with _client(app, handler) as c:
        assert c.get("/api/rpc/command/x").status_code == 200
    assert seen["url"].startswith(GATEWAY)


def test_no_request_can_choose_the_upstream():
    """Penpot is selected by path and by nothing else — a caller must never be
    able to steer a *non*-Penpot path into it."""
    seen, handler = _recorder()
    with _client(_app(), handler) as c:
        for req in (
            {"url": "/ui/drawio/?upstream=" + PENPOT},
            {"url": "/ui/drawio/", "headers": {"X-Awm-As": "user:steven"}},
            {"url": "/ui/drawio/", "headers": {"X-Forwarded-Host": "penpot"}},
            {"url": "/ui/drawio/", "headers": {"Host": "penpot"}},
        ):
            seen.clear()
            c.get(req["url"], headers=req.get("headers"))
            assert seen["url"].startswith(GATEWAY), req


def test_the_browsers_own_identity_header_is_discarded():
    """`X-Awm-As` is the edge's to write, not the browser's — forging it would
    let a caller impersonate another user's Penpot session."""
    seen, handler = _recorder()
    with _client(_app(), handler) as c:
        c.get(penpot.SHELL, headers={"X-Awm-As": "peer"})
    assert seen["headers"]["x-awm-as"] == "user:tony"


def test_the_real_client_ip_reaches_penpot():
    seen, handler = _recorder()
    with _client(_app(), handler) as c:
        c.get(penpot.SHELL)
    assert "x-forwarded-for" in seen["headers"]


# -- what makes this a bespoke upstream rather than a kind="url" mount --------

def test_cookies_authorization_and_origin_survive_the_proxy():
    """Prevents the exact regression this task exists to guard against: the
    generic kind="url" mount strips cookies/Authorization by design, which
    would mean every Penpot request arrives logged out. A real Penpot session
    lives in a cookie set by Penpot's own login, so losing it here breaks the
    session end to end with no visible error — the page just never stays
    signed in."""
    seen, handler = _recorder()
    with _client(_app(), handler) as c:
        c.cookies.set("penpot-session", "abc123", domain="nexus.example")
        r = c.get(penpot.SHELL, headers={
            "Authorization": "Bearer some-penpot-token",
            "Origin": "https://nexus.example",
        })
    assert r.status_code == 200
    assert "penpot-session=abc123" in seen["headers"]["cookie"]
    assert seen["headers"]["authorization"] == "Bearer some-penpot-token"
    assert seen["headers"]["origin"] == "https://nexus.example"


def test_hop_by_hop_headers_do_not_survive_the_proxy():
    """RFC 7230 §6.1: forwarding these verbatim would let a client hand the
    upstream a forged Host, or values that corrupt the connection-reuse
    machinery between the edge and Penpot. (``connection`` itself is httpx's
    own to set on the upstream leg regardless of input — what matters is that
    the caller's value never reaches it, checked by inequality rather than
    absence.)"""
    seen, handler = _recorder()
    with _client(_app(), handler) as c:
        c.get(penpot.SHELL, headers={
            "Connection": "attacker-value", "Te": "trailers",
            "Transfer-Encoding": "chunked", "Upgrade": "websocket",
            "Host": "attacker.example",
        })
    assert seen["headers"]["connection"] != "attacker-value"
    for h in ("te", "transfer-encoding", "upgrade"):
        assert h not in seen["headers"], h
    assert seen["headers"]["host"] != "attacker.example"


# -- the edge's own answers ---------------------------------------------------

def test_a_trailing_slash_redirects_permanently():
    _, handler = _recorder()
    with _client(_app(), handler) as c:
        r = c.get(penpot.SHELL_SLASH)
    assert r.status_code == 308 and r.headers["location"] == penpot.SHELL


def test_penpot_keeps_its_own_logout():
    """Unlike the vault's `/logout` (claimed by the edge because Trilium's own
    login is off), Penpot's `/logout` must never be intercepted — Penpot's own
    login is real, and hijacking its logout would sign a user out of Penpot
    without them asking, or worse, silently no-op their real logout click.

    Checked on the mesh profile (no allow-list) so this isolates the routing
    question from the public profile's separate `/logout` gate — `/logout`
    is not itself on the public OPEN list, which would 404 it before routing
    is ever consulted and prove nothing about the hijack this pins.
    """
    seen, handler = _recorder()
    with _client(_app(profile=None), handler) as c:
        r = c.get("/logout")
    assert r.status_code != 302 or r.headers.get("location") != "/__auth/logout"
    # It falls through to the ordinary catch-all proxy, to the gateway (the
    # only upstream that owns an unlisted path like this one).
    assert seen.get("url", "").startswith(GATEWAY)


# -- who gets in --------------------------------------------------------------

def test_signed_out_penpot_asks_for_a_login():
    _, handler = _recorder()
    with _client(_app(), handler, authed=False) as c:
        r = c.get(penpot.SHELL, headers={"Accept": "text/html"})
    assert r.status_code == 200 and "username" in r.text


def test_a_peer_bearer_does_not_get_penpot():
    _, handler = _recorder()
    with _client(_app(sub="peer"), handler) as c:
        assert c.get(penpot.SHELL).status_code == 404


def test_a_peer_is_refused_on_the_mesh_profile_too():
    """The mesh edge consults no allow-list, so the check has to be in the
    branch rather than in policy — this is that check."""
    _, handler = _recorder()
    with _client(_app(sub="peer", profile=None), handler) as c:
        assert c.get(penpot.SHELL).status_code == 404


# -- when penpot is not there --------------------------------------------------

def test_a_penpot_that_is_not_listening_says_try_again():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    with _client(_app(), handler) as c:
        r = c.get(penpot.SHELL, headers={"Accept": "text/html"})
    assert r.status_code == 503
    assert r.headers["retry-after"] == "5"
    assert "not answering yet" in r.text


def test_the_gateway_being_down_is_still_a_502():
    """Penpot's failure mode must not swallow the gateway's."""
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    with _client(_app(), handler) as c:
        r = c.get("/ui/drawio/")
    assert r.status_code == 502


# -- the WebSocket leg (Penpot's collab socket) -------------------------------

def _ws_target(monkeypatch, app, path: str) -> dict:
    captured: dict = {}

    async def _fake_connect(url, *, additional_headers=None, **kw):
        captured["url"] = url
        captured["headers"] = {k.lower(): v
                               for k, v in (additional_headers or {}).items()}
        raise RuntimeError("upstream refused")  # closes the handshake cleanly

    monkeypatch.setattr(proxy.websockets, "connect", _fake_connect)
    _, handler = _recorder()
    with _client(app, handler) as c:
        try:
            with c.websocket_connect(path, headers={"cookie": f"{COOKIE_NAME}={TOKEN}"}):
                pass
        except Exception:  # noqa: BLE001 — the reject is the expected path
            pass
    return captured


def test_penpots_collab_socket_follows_its_shell(monkeypatch):
    """If this routes anywhere but Penpot, a shared board's live edits never
    arrive and nothing anywhere says so — a keepalive would still pass."""
    seen = _ws_target(monkeypatch, _app(), "/ws/notifications")
    assert seen["url"] == "ws://127.0.0.1:9001/ws/notifications"


def test_an_awm_socket_still_reaches_the_gateway(monkeypatch):
    seen = _ws_target(monkeypatch, _app(profile=None), "/svc/drawio/emit/drawio:tony:x")
    assert seen["url"].startswith("ws://127.0.0.1:7819")


def test_the_socket_carries_the_verified_identity(monkeypatch):
    seen = _ws_target(monkeypatch, _app(), "/ws/notifications")
    assert seen["headers"]["x-awm-as"] == "user:tony"


# -- the root actually reaches Penpot, not just `owns()` ----------------------

def _root_app(*, profile=None, penpot_root=True, landing=True):
    app = proxy.build_app(GATEWAY + "/", "/dev/null", profile=profile,
                          landing=landing, penpot_upstream=PENPOT,
                          penpot_root=penpot_root)
    app.state.gate = _Gate("tony")
    return app


def test_a_request_for_the_root_reaches_penpot_when_it_owns_the_root():
    """`penpot.owns("/", at_root=True)` returning True proves nothing on its
    own: `build_app` registers its own `/` route ahead of the catch-all that
    consults `owns`, and Starlette takes the first full match. Asserting on
    `owns` alone let a change ship that made Penpot *less* reachable — the
    landing page answered `/` with a clean 200 and Penpot was never asked."""
    seen, handler = _recorder()
    with _client(_root_app(), handler) as c:
        resp = c.get("/")
    assert resp.status_code == 200
    assert seen["url"] == PENPOT + "/"


def test_the_root_still_belongs_to_the_edge_when_penpot_is_not_at_root():
    """The default must not take `/` away from a listener that merely has
    Penpot's asset paths enabled. The landing page reaches the gateway for
    its own index, so what matters is that Penpot is not the upstream."""
    seen, handler = _recorder()
    with _client(_root_app(penpot_root=False), handler) as c:
        resp = c.get("/")
    assert resp.status_code == 200
    assert not seen.get("url", "").startswith(PENPOT)


def test_the_public_home_redirect_yields_to_penpot_at_root():
    seen, handler = _recorder()
    with _client(_root_app(profile="public"), handler) as c:
        resp = c.get("/")
    assert resp.status_code == 200
    assert seen["url"] == PENPOT + "/"


def test_the_shell_path_redirects_to_the_root_when_penpot_owns_it():
    """Serving the shell at `/penpot` would answer 200 with a page whose own
    router cannot parse that pathname, so it renders the login screen however
    valid the session is. A redirect is the only honest answer."""
    seen, handler = _recorder()
    with _client(_root_app(), handler) as c:
        resp = c.get("/penpot")
    assert resp.status_code == 302
    assert resp.headers["location"] == "/"


def test_penpot_at_root_does_not_shadow_the_edges_own_auth_surface():
    seen, handler = _recorder()
    with _client(_root_app(), handler) as c:
        resp = c.get("/__auth/whoami")
    assert resp.status_code == 200
    assert seen == {}
