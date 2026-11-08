"""The public door: identity is stamped by the edge, and only the listed
paths exist.

What these pin: the browser's ``X-Awm-As`` is overwritten (HTTP and WS) with
the session's verified subject; a browser with no session goes to alcor's
passkey login; ``/__auth/whoami`` answers the real
subject; on the public profile every unlisted path is 404 signed in or not,
and user-scoped paths admit only their own user; with the profile unset the
front is the transparent proxy it always was (every older test in this dir).
"""

from __future__ import annotations

import json

import httpx
import pytest
from starlette.testclient import TestClient

from awm.httpsfront import policy, proxy
from awm.tlsfront import proxy as tlsproxy
from awm.tlsfront.auth import COOKIE_NAME

pytestmark = [pytest.mark.unit, pytest.mark.smoke]

TONY, STEVEN = "tok-tony", "tok-steven"
SUBS = {TONY: "tony", STEVEN: "steven"}


class _Gate:
    """Two sessions."""

    async def authenticate(self, *, cookie=None, bearer=None, shape=None):
        if cookie in SUBS:
            return True, None, SUBS[cookie]
        return False, None, None

    async def session_ttl_seconds(self):
        return 3600.0


async def _body():
    yield b"ok"


def _app(profile=None):
    app = proxy.build_app("http://127.0.0.1:1/", "/nonexistent/ca.pem",
                          profile=profile)
    app.state.gate = _Gate()
    return app


def _client(app, token=TONY):
    c = TestClient(app, base_url="https://front.example", follow_redirects=False)
    if token:
        c.cookies.set(COOKIE_NAME, token, domain="front.example")
    return c


def _capture(c):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update({k.lower(): v for k, v in request.headers.items()})
        seen["path"] = request.url.path
        return httpx.Response(200, content=_body())

    c.app.state.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return seen


# --- identity stamping (every profile) -------------------------------------

def test_http_overwrites_the_browsers_x_awm_as():
    with _client(_app()) as c:
        seen = _capture(c)
        r = c.get("/tools", headers={"X-Awm-As": "user:steven"})
    assert r.status_code == 200
    assert seen["x-awm-as"] == "user:tony"


def test_a_forged_peer_header_never_reaches_the_gateway():
    """A browser (or another node) naming itself `peer:mira` gets overwritten."""
    with _client(_app()) as c:
        seen = _capture(c)
        c.get("/tools", headers={"X-Awm-As": "peer:mira"})
    assert seen["x-awm-as"] == "user:tony"


def test_ws_forwards_only_the_verified_identity(monkeypatch):
    fwd = {}

    class _Up:
        subprotocol = None

        async def close(self):
            pass

        def __aiter__(self):
            return self

        async def __anext__(self):
            import asyncio
            await asyncio.Event().wait()  # stay open until the browser side closes

        async def send(self, _):
            pass

    async def connect(url, additional_headers=None, **kw):
        fwd.update(additional_headers or {})
        return _Up()

    monkeypatch.setattr(tlsproxy.websockets, "connect", connect)
    with _client(_app()) as c:
        with c.websocket_connect("/hub/emit/journal/posts",
                                 headers={"X-Awm-As": "user:steven",
                                          "Cookie": f"{COOKIE_NAME}={TONY}"}):
            pass
    assert fwd["X-Awm-As"] == "user:tony"
    assert "x-awm-as" not in fwd


def test_whoami_answers_the_real_subject():
    with _client(_app(), token=STEVEN) as c:
        assert c.get("/__auth/whoami").json() == {"user": "steven"}
    with _client(_app(), token=None) as c:
        assert c.get("/__auth/whoami").status_code == 401


def test_logout_clears_both_cookies():
    with _client(_app()) as c:
        r = c.post("/__auth/logout")
    setc = "\n".join(r.headers.get_list("set-cookie"))
    assert "awm_session=" in setc and "awm_as=" in setc


# --- the public profile ------------------------------------------------------

DENIED = [
    "/hub/services", "/hub/plugins", "/tools", "/svc/scopes/fn/scope_search",
    "/svc/auth/fn/password", "/svc/auth/fn/verify",
    "/__auth/link", "/__auth/login", "/ca.crt", "/ca.pem",
    "/ui/agents/", "/p/dashboard/dashboard/", "/p/httpsfront/login/",
    "/files/", "/s/fileviewer/", "/s/fileviewer/projects/awm/dev/x",
    "/s/claude-science/mcp",
    "/__landing/tags",
    # `/invoke` is a POST. Its body is what the door checks; see
    # test_public_invoke_* below.
    "/invoke",
    # notes and drawio left the public host, then awm (install.md R134). These
    # are here rather than merely absent because they are the paths whose
    # verdict *changed*: an allow-list that only records what is open cannot
    # fail when something is re-opened by accident, and this is the door.
    "/ui/notes/", "/svc/notes/fn/list", "/svc/notes/emit/note:tony:1",
    "/ui/drawio/", "/drawio-app/index.html", "/svc/drawio/fn/save",
    "/svc/drawio/emit/drawio:tony:x.drawio",
    # The vault's own surface that a browser must never reach through us. Each
    # is a live Trilium route; see awm.httpsfront.vault.NOT_FORWARDED.
    "/etapi/app-info", "/custom/x", "/mcp", "/share/abc",
    "/login", "/setup", "/set-password", "/robots.txt",
    # The trilium page's old addresses, which the gateway no longer serves.
    "/ui/trilium", "/ui/trilium/", "/svc/trilium/fn/status",
    "/svc/trilium/fn/restore",
    # Adjacent to an open mount, not inside it. The prefix is a
    # ``startswith`` against a deny-by-default door, so writing it bare would
    # hand this path to the internet on the strength of its name alone.
    "/p/trilium/triliumXYZ", "/p/trilium/trilium-admin/x", "/p/trilium/",
]
ALLOWED = [
    "/p/trilium/trilium", "/p/trilium/trilium/", "/p/trilium/trilium/assets/index.js",
]


@pytest.mark.parametrize("path", DENIED)
def test_denied_paths_are_404_signed_in_or_not(path):
    for token in (TONY, None):
        with _client(_app("public"), token=token) as c:
            _capture(c)
            r = c.get(path, headers={"Accept": "text/html"})
        assert r.status_code == 404, (path, token)


@pytest.mark.parametrize("path", ALLOWED)
def test_allowed_paths_proxy_when_signed_in(path):
    with _client(_app("public")) as c:
        seen = _capture(c)
        assert c.get(path).status_code == 200
    assert seen["path"] == path


def test_allowed_paths_want_a_login_when_signed_out():
    with _client(_app("public"), token=None) as c:
        r = c.get("/p/trilium/trilium/", headers={"Accept": "text/html"})
        assert r.status_code == 302 and r.headers["location"].endswith(
            "/signin?next=https%3A%2F%2Ffront.example%2Fp%2Ftrilium%2Ftrilium%2F")
        assert c.post("/invoke", json=_call("status")).status_code == 401


def _call(verb, **extra):
    """The body the trilium page's `tool('trilium').call(verb)` posts."""
    return {"name": "trilium", "args": {"verb": verb, "args": {}, **extra}}


def _capture_body(c):
    seen = _capture(c)
    handler = c.app.state.client._transport.handler

    def with_body(request: httpx.Request) -> httpx.Response:
        seen["body"] = request.read()
        return handler(request)

    c.app.state.client = httpx.AsyncClient(transport=httpx.MockTransport(with_body))
    return seen


@pytest.mark.parametrize("verb", sorted(policy.TRILIUM_OPEN_FNS))
def test_public_invoke_admits_the_trilium_pages_read_verbs(verb):
    with _client(_app("public")) as c:
        seen = _capture_body(c)
        r = c.post("/invoke", json=_call(verb),
                   headers={"Referer": "https://front.example/p/trilium/trilium/"})
    assert r.status_code == 200
    assert seen["path"] == "/invoke"
    assert json.loads(seen["body"]) == _call(verb)
    assert seen["x-awm-as"] == "user:tony"
    assert seen["referer"] == "https://front.example/p/trilium/trilium/"


@pytest.mark.parametrize("body", [
    _call("restore"), _call("stop"), _call("logs"), _call("export"),
    _call("slice_resolve"),
    _call("status", peer="mira"),
    {"name": "trilium_status", "args": {}},
    {"name": "journal", "args": {"verb": "status", "args": {}}},
    {"name": "trilium", "args": {"verb": "status", "args": []}},
    {"name": "trilium", "args": {"verb": "status"}, "peer": "mira"},
    [], "status",
])
def test_public_invoke_refuses_every_other_call(body):
    with _client(_app("public")) as c:
        seen = _capture(c)
        assert c.post("/invoke", json=body).status_code == 404
    assert "path" not in seen


def test_public_invoke_refuses_a_body_that_is_not_json_or_too_large():
    with _client(_app("public")) as c:
        seen = _capture(c)
        assert c.post("/invoke", content=b"{not json").status_code == 404
        big = json.dumps({"name": "trilium", "args": {
            "verb": "status", "args": {"pad": "x" * policy.INVOKE_BODY_CAP}}})
        assert c.post("/invoke", content=big).status_code == 404
    assert "path" not in seen


def test_public_invoke_has_no_socket():
    from starlette.websockets import WebSocketDisconnect

    with _client(_app("public")) as c:
        with pytest.raises(WebSocketDisconnect):
            with c.websocket_connect("/invoke"):
                pass


def test_user_scoped_paths_admit_only_their_user():
    with _client(_app("public")) as c:
        _capture(c)
        assert c.get("/s/fileviewer/projects/userdata/tony/notes/a.md").status_code == 200
        assert c.get("/s/fileviewer/projects/userdata/steven/notes/a.md").status_code == 404
        assert c.get("/s/fileviewer/projects/userdata/tony").status_code == 200
        assert c.get("/files/projects/userdata/tony/notes/a.md").status_code == 404


def test_no_emit_topic_is_public():
    assert not policy.allows("/svc/drawio/emit/drawio:tony:x.drawio", "tony")
    assert not policy.allows("/svc/notes/emit/note:tony:abc", "tony")


def test_root_redirects_to_the_vault_or_asks_for_login():
    with _client(_app("public")) as c:
        r = c.get("/")
        assert r.status_code == 302 and r.headers["location"] == "/trilium/"
    with _client(_app("public"), token=None) as c:
        r = c.get("/", headers={"Accept": "text/html"})
        assert r.status_code == 302 and "/signin?next=" in r.headers["location"]


def test_serve_without_tls_binds_loopback(monkeypatch):
    captured = {}

    class _Cfg:
        def __init__(self, app, **kw):
            captured.update(kw)

    class _Srv:
        def __init__(self, cfg):
            pass

        def run(self):
            pass

    monkeypatch.setattr(tlsproxy.uvicorn, "Config", _Cfg)
    monkeypatch.setattr(tlsproxy.uvicorn, "Server", _Srv)
    proxy.serve(port=8444, cert="", key="", ca="", upstream="http://127.0.0.1:1/",
                profile="public", tls=False)
    assert captured["host"] == "127.0.0.1"
    assert captured["proxy_headers"] is True
    assert captured["forwarded_allow_ips"] == "127.0.0.1"
    assert "ssl_certfile" not in captured
    assert captured["log_level"] == "warning"
