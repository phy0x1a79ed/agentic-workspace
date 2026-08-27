"""The vault as a second upstream on the same listener.

What these pin: the mount is stripped and nothing else is; the bare name is a
redirect and never the shell; the path arrives at the upstream *byte for byte*,
which is what makes search work and what keeps the refusal list unreachable; the
WebSocket takes the same route as HTTP; the upstream is chosen by *path*, never
by anything a caller can send; and the failure a person is most likely to meet —
a vault that has not finished starting — reads as "try again" rather than "awm is
broken".
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


@pytest.mark.parametrize("inner", ["/api/tree", "/src/a.js", "/bootstrap",
                                  "/assets/v1/x.css", "/favicon.ico"])
def test_mounted_paths_reach_the_vault_with_the_mount_taken_off(inner):
    seen, handler = _recorder()
    with _client(_app(), handler) as c:
        assert c.get(vault.SHELL + inner.lstrip("/")).status_code == 200
    assert seen["url"] == VAULT + inner


@pytest.mark.parametrize("path", ["/api/tree", "/src/a.js", "/bootstrap",
                                  "/assets/v1/x.css", "/favicon.ico"])
def test_the_same_names_at_the_site_root_are_not_the_vaults(path):
    """The mount is what gives awm its own root-level surface back. Off the
    public profile these reach the gateway; on it they are simply not there."""
    seen, handler = _recorder()
    with _client(_app(profile=None), handler) as c:
        assert c.get(path).status_code == 200
    assert seen["url"].startswith(GATEWAY)
    with _client(_app(), handler) as c:
        assert c.get(path).status_code == 404


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

def test_the_bare_name_redirects_permanently_onto_the_mount():
    """The opposite of what this used to assert, and the reason is the whole of
    T2: from `/trilium` the shell's relative references resolve to the site
    root, and Trilium's hashchange parser wants the literal `/#root`."""
    _, handler = _recorder()
    with _client(_app(), handler) as c:
        r = c.get(vault.SHELL_BARE)
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
        r = c.get(vault.LOGOUT)
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
    shell served at /trilium/ opens its socket there — which has to arrive at
    the upstream's root, exactly as the shell request does."""
    seen = _ws_target(monkeypatch, _app(), vault.SHELL)
    assert seen["url"] == "ws://127.0.0.1:12511/"


def test_an_awm_socket_still_reaches_the_gateway(monkeypatch):
    seen = _ws_target(monkeypatch, _app(profile=None), "/svc/drawio/emit/drawio:tony:x")
    assert seen["url"].startswith("ws://127.0.0.1:7819")


def test_the_socket_carries_the_verified_identity(monkeypatch):
    seen = _ws_target(monkeypatch, _app(), vault.SHELL)
    assert seen["headers"]["x-awm-as"] == "user:tony"


# -- the path, byte for byte --------------------------------------------------
#
# The edge routes on the *decoded* path and forwards the *raw* one. Building the
# upstream URL out of the decoded string is what truncated every attribute
# search in the vault, and — the same defect wearing a different hat — what made
# the refusal list bypassable. These pin both halves.

@pytest.mark.parametrize("encoded", [
    "%23calendarRoot",            # `#` — the one that broke search
    "%23book%20AND%20%23read",    # `#` and spaces together
    "a%3Fb",                      # `?` — a query separator inside the path
    "a%25b",                      # a literal percent
    "%C3%A9t%C3%A9",              # non-ASCII
])
def test_an_encoded_path_reaches_the_vault_unchanged(encoded):
    """httpx reparses a URL *string*: it takes everything after a `#` as a
    fragment and never sends it, so `/api/search/%23x` arrived as `/api/search/`
    and Trilium answered `Router not found`. Handing it the raw bytes instead is
    the fix, and this is the assertion that says so."""
    seen, handler = _recorder()
    with _client(_app(), handler) as c:
        assert c.get(vault.SHELL + "api/search/" + encoded).status_code == 200
    assert seen["url"] == VAULT + "/api/search/" + encoded


def test_the_query_string_survives_alongside_it():
    """`copy_with(raw_path=…)` carries the query, so the one thing that must not
    happen is appending it a second time."""
    seen, handler = _recorder()
    with _client(_app(), handler) as c:
        c.get(vault.SHELL + "api/search/%23x?fastSearch=false&debug=1")
    assert seen["url"] == VAULT + "/api/search/%23x?fastSearch=false&debug=1"


@pytest.mark.parametrize("profile", ["public", None])
@pytest.mark.parametrize("path", [
    "/trilium/api/%2e%2e/etapi/app-info",
    "/trilium/%2e%2e/%2e%2e/etc/passwd",
    "/ui/drawio/%2e%2e/hub/services",
])
def test_a_path_that_climbs_out_is_refused(profile, path):
    """This was live. `/trilium/api/%2e%2e/etapi/app-info` decodes to a path
    starting inside the vault, so it classified as the vault's — and httpx
    normalised the `..` away before sending, delivering it to the ETAPI, which
    has no authentication at all on this deployment because Trilium's own login
    is off. A 404 here, on both profiles, is the whole of the fix."""
    seen, handler = _recorder()
    with _client(_app(profile=profile), handler) as c:
        assert c.get(path).status_code == 404
    assert "url" not in seen, "nothing may be forwarded"


def test_an_unencoded_dot_dot_is_refused_too():
    """Asserted on the guard rather than through the client, because every HTTP
    client — the test client, and every browser — resolves a literal `../` away
    before it is ever sent. Only a hand-written request can carry one, which is
    exactly the caller the guard is for."""
    assert proxy._re_segments(b"/trilium/api/../etapi/app-info",
                              "/trilium/api/../etapi/app-info")
    assert not proxy._re_segments(b"/trilium/api/notes/..x", "/trilium/api/notes/..x")
    assert not proxy._re_segments(b"/trilium/api/search/%23x", "/trilium/api/search/#x")


@pytest.mark.parametrize("path", [
    "/trilium/api%2Fnotes",
    "/trilium/api%2fnotes",
    "/trilium/api%5cnotes",
    "/ui/drawio%2f..%2fhub",
])
def test_an_encoded_separator_is_refused(path):
    """One segment to the router, two to the upstream. Nothing this edge serves
    needs it, and letting it through means routing and forwarding disagree about
    what the path is."""
    seen, handler = _recorder()
    with _client(_app(), handler) as c:
        assert c.get(path).status_code == 404
    assert "url" not in seen


def test_the_mount_must_be_in_the_bytes_not_only_the_decoding():
    """`/%74rilium/api/tree` decodes to a vault path but does not carry the
    mount, so the byte-level strip cannot take it off — forwarding it would send
    Trilium a path it does not serve, from a classification that said it did."""
    seen, handler = _recorder()
    with _client(_app(), handler) as c:
        assert c.get("/%74rilium/api/tree").status_code == 404
    assert "url" not in seen


def test_a_refused_route_stays_refused_under_the_mount(monkeypatch):
    """Everything below the mount is the vault's by default, so this list is the
    only thing between a browser and Trilium's unauthenticated ETAPI."""
    seen, handler = _recorder()
    with _client(_app(), handler) as c:
        for inner in vault.NOT_FORWARDED:
            r = c.get(vault.SHELL + inner.lstrip("/"))
            assert r.status_code in (302, 404), (inner, r.status_code)
            assert "url" not in seen, inner


def test_the_sockets_path_arrives_raw_too(monkeypatch):
    """The WS leg builds its URI by string concatenation, so it needs the same
    bytes the HTTP leg gets — and the same refusal, or the guard is half a
    guard."""
    seen = _ws_target(monkeypatch, _app(), vault.SHELL + "api/x%23y")
    assert seen["url"] == "ws://127.0.0.1:12511/api/x%23y"


def test_a_socket_that_climbs_out_is_refused(monkeypatch):
    seen = _ws_target(monkeypatch, _app(), "/trilium/api/%2e%2e/etapi/app-info")
    assert "url" not in seen, "the handshake must not reach any upstream"
