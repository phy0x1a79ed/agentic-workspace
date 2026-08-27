"""The vault as a second upstream on the same listener.

What these pin: the shell is rewritten to the upstream's root and nothing else
is; a trailing slash is a redirect and never the shell; the WebSocket takes the
same route as HTTP; the upstream is chosen by *path*, never by anything a
caller can send; and the failure a person is most likely to meet — a vault that
has not finished starting — reads as "try again" rather than "awm is broken".
"""

from __future__ import annotations

from contextlib import contextmanager

import httpx
import pytest
from starlette.testclient import TestClient

from awm.httpsfront import proxy, vault
from awm.httpsfront.auth import COOKIE_NAME

pytestmark = [pytest.mark.unit, pytest.mark.smoke]

TOKEN = "signed.session.token"
GATEWAY = "http://127.0.0.1:7819"
VAULT = "http://127.0.0.1:12511"


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


def _app(*, profile="public", sub="tony", with_vault=True):
    app = proxy.build_app(GATEWAY + "/", "/dev/null", profile=profile,
                          vault_upstream=VAULT if with_vault else None)
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
        assert c.get(vault.SHELL).status_code == 200
    assert seen["url"] == VAULT + "/"


@pytest.mark.parametrize("path", ["/api/tree", "/src/a.js", "/bootstrap",
                                  "/assets/v1/x.css", "/favicon.ico"])
def test_root_level_vault_paths_go_to_the_vault_unchanged(path):
    seen, handler = _recorder()
    with _client(_app(), handler) as c:
        assert c.get(path).status_code == 200
    assert seen["url"] == VAULT + path


@pytest.mark.parametrize("path", ["/ui/drawio/", "/svc/drawio/fn/save"])
def test_awm_paths_still_go_to_the_gateway(path):
    seen, handler = _recorder()
    with _client(_app(), handler) as c:
        assert c.get(path).status_code == 200
    assert seen["url"].startswith(GATEWAY)


def test_without_a_vault_upstream_nothing_changes():
    """The parameter is off by default, so every existing caller is unaffected."""
    seen, handler = _recorder()
    app = _app(with_vault=False, profile=None)
    with _client(app, handler) as c:
        assert c.get("/api/tree").status_code == 200
    assert seen["url"].startswith(GATEWAY)


def test_no_request_can_choose_the_upstream():
    """The vault is selected by path and by nothing else.

    There is only one vault, so there is no "other vault" to reach — but the
    edge must also never let a caller steer a *non*-vault path into it, which
    is the shape this would take if the upstream were ever taken from input.
    """
    seen, handler = _recorder()
    with _client(_app(), handler) as c:
        for req in (
            {"url": "/ui/drawio/?upstream=" + VAULT},
            {"url": "/ui/drawio/", "headers": {"X-Awm-As": "user:steven"}},
            {"url": "/ui/drawio/", "headers": {"X-Forwarded-Host": "vault"}},
            {"url": "/ui/drawio/", "headers": {"Host": "vault"}},
        ):
            seen.clear()
            c.get(req["url"], headers=req.get("headers"))
            assert seen["url"].startswith(GATEWAY), req


def test_the_browsers_own_identity_header_is_discarded():
    """`X-Awm-As` is the edge's to write. If a caller could set it, the
    operator-only gate in the trilium service would be forgeable."""
    seen, handler = _recorder()
    with _client(_app(), handler) as c:
        c.get(vault.SHELL, headers={"X-Awm-As": "peer"})
    assert seen["headers"]["x-awm-as"] == "user:tony"


def test_the_real_client_ip_reaches_the_vault():
    """Trilium rate-limits the shell per IP. If X-Forwarded-For went missing,
    every visitor would collapse onto one bucket and the 101st load in the
    window would get a bare 429 that looks like an outage."""
    seen, handler = _recorder()
    with _client(_app(), handler) as c:
        c.get(vault.SHELL)
    assert "x-forwarded-for" in seen["headers"]


# -- the edge's own answers ---------------------------------------------------

def test_a_trailing_slash_redirects_permanently():
    _, handler = _recorder()
    with _client(_app(), handler) as c:
        r = c.get(vault.SHELL_SLASH)
    assert r.status_code == 308 and r.headers["location"] == vault.SHELL


def test_the_manifest_is_ours_not_the_vaults():
    seen, handler = _recorder()
    with _client(_app(), handler) as c:
        r = c.get(vault.MANIFEST)
    assert r.status_code == 200
    assert r.json()["start_url"] == vault.SHELL
    assert "url" not in seen, "the manifest must not be proxied"


def test_the_vaults_logout_ends_the_awm_session():
    _, handler = _recorder()
    with _client(_app(), handler) as c:
        r = c.get("/logout")
    assert r.status_code == 302 and r.headers["location"] == "/__auth/logout"


def test_signing_in_and_out_clears_the_vaults_cookies():
    """Trilium's cookies are not namespaced by person and sit at path=/, so a
    second person on the same browser would otherwise send the first's."""
    _, handler = _recorder()
    with _client(_app(), handler) as c:
        setc = c.get("/__auth/logout").headers.get_list("set-cookie")
    assert any("trilium.sid=" in h for h in setc)
    assert any("trilium-csrf=" in h for h in setc)


# -- who gets in --------------------------------------------------------------

def test_signed_out_the_vault_asks_for_a_login():
    _, handler = _recorder()
    with _client(_app(), handler, authed=False) as c:
        r = c.get(vault.SHELL, headers={"Accept": "text/html"})
    assert r.status_code == 200 and "username" in r.text


def test_a_peer_bearer_does_not_get_the_vault():
    _, handler = _recorder()
    with _client(_app(sub="peer"), handler) as c:
        assert c.get(vault.SHELL).status_code == 404


def test_a_peer_is_refused_on_the_mesh_profile_too():
    """The mesh edge consults no allow-list, so the check has to be in the
    branch rather than in policy — this is that check."""
    _, handler = _recorder()
    with _client(_app(sub="peer", profile=None), handler) as c:
        assert c.get(vault.SHELL).status_code == 404


# -- when the vault is not there ----------------------------------------------

def test_a_vault_that_is_not_listening_says_try_again():
    """503 + Retry-After, not the generic 502. A cold child takes up to two
    minutes to bind, and `upstream gateway unreachable` reads as awm being
    broken when the true answer is "wait five seconds"."""
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    with _client(_app(), handler) as c:
        r = c.get(vault.SHELL, headers={"Accept": "text/html"})
    assert r.status_code == 503
    assert r.headers["retry-after"] == "5"
    assert "not answering yet" in r.text


def test_the_gateway_being_down_is_still_a_502():
    """The vault's failure mode must not swallow the gateway's."""
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    with _client(_app(), handler) as c:
        r = c.get("/ui/drawio/")
    assert r.status_code == 502


# -- the WebSocket leg --------------------------------------------------------
#
# The vault holds a socket open for every change it renders, so this leg is not
# optional decoration: if it routes anywhere but the vault, the page loads, the
# socket connects to the wrong upstream, and nothing anywhere says so. A
# keepalive would still pass.

def _ws_target(monkeypatch, app, path: str) -> dict:
    """Capture the URL _ws_proxy would dial, then abort the handshake."""
    captured: dict = {}

    async def _fake_connect(url, *, additional_headers=None, **kw):
        captured["url"] = url
        captured["headers"] = {k.lower(): v
                               for k, v in (additional_headers or {}).items()}
        raise RuntimeError("upstream refused")  # closes the handshake cleanly

    monkeypatch.setattr(proxy.websockets, "connect", _fake_connect)
    _, handler = _recorder()
    # TestClient does not apply its cookie jar to a WS handshake, so the edge
    # session goes on the request directly.
    with _client(app, handler) as c:
        try:
            with c.websocket_connect(path, headers={"cookie": f"{COOKIE_NAME}={TOKEN}"}):
                pass
        except Exception:  # noqa: BLE001 — the reject is the expected path
            pass
    return captured


def test_the_vaults_socket_follows_its_shell(monkeypatch):
    """The client derives its socket URL from the page's own pathname, so a
    shell served at /vault opens its socket at /vault — which has to arrive at
    the upstream's root, exactly as the shell request does."""
    seen = _ws_target(monkeypatch, _app(), vault.SHELL)
    assert seen["url"] == "ws://127.0.0.1:12511/"


def test_an_awm_socket_still_reaches_the_gateway(monkeypatch):
    seen = _ws_target(monkeypatch, _app(profile=None), "/svc/drawio/emit/drawio:tony:x")
    assert seen["url"].startswith("ws://127.0.0.1:7819")


def test_the_socket_carries_the_verified_identity(monkeypatch):
    seen = _ws_target(monkeypatch, _app(), vault.SHELL)
    assert seen["headers"]["x-awm-as"] == "user:tony"
