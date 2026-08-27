"""The public door: identity is stamped by the edge, and only the listed
paths exist.

What these pin: the browser's ``X-Awm-As`` is overwritten (HTTP and WS) with
the session's verified subject; ``/__auth/login`` takes a username, sets both
cookies, and relays a lockout as 429; ``/__auth/whoami`` answers the real
subject; on the public profile every unlisted path is 404 signed in or not,
and user-scoped paths admit only their own user; with the profile unset the
front is the transparent proxy it always was (every older test in this dir).
"""

from __future__ import annotations

import httpx
import pytest
from starlette.testclient import TestClient

from awm.httpsfront import policy, proxy
from awm.httpsfront.auth import AS_COOKIE_NAME, COOKIE_NAME

pytestmark = [pytest.mark.unit, pytest.mark.smoke]

TONY, STEVEN = "tok-tony", "tok-steven"
SUBS = {TONY: "tony", STEVEN: "steven"}


class _Gate:
    """Two sessions, one peer bearer, one known login, one locked login."""

    async def authenticate(self, *, cookie=None, bearer=None):
        if bearer == "peer-cred":
            return True, None, "peer"
        if cookie in SUBS:
            return True, None, SUBS[cookie]
        return False, None, None

    async def verify_login(self, *, username, password, client_ip):
        if username == "locked":
            return {"ok": False, "locked": True, "retry_after": 321}
        if username == "tony" and password == "pw":
            return {"ok": True, "token": TONY, "sub": "tony"}
        return {"ok": False}

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
        r = c.get("/svc/notes/fn/list", headers={"X-Awm-As": "user:steven"})
    assert r.status_code == 200
    assert seen["x-awm-as"] == "user:tony"


def test_a_peer_bearer_is_stamped_as_peer():
    with _client(_app(), token=None) as c:
        seen = _capture(c)
        c.get("/svc/notes/fn/list", headers={
            "Authorization": "Bearer peer-cred", "X-Awm-As": "user:tony"})
    assert seen["x-awm-as"] == "peer"


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

    monkeypatch.setattr(proxy.websockets, "connect", connect)
    with _client(_app()) as c:
        with c.websocket_connect("/svc/notes/emit/note:tony:1",
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


def test_login_with_username_sets_both_cookies():
    with _client(_app(), token=None) as c:
        r = c.post("/__auth/login", json={"username": "tony", "password": "pw"})
    assert r.status_code == 200 and r.json()["user"] == "tony"
    assert r.cookies[COOKIE_NAME] == TONY
    assert r.cookies[AS_COOKIE_NAME] == "tony"
    setc = "\n".join(r.headers.get_list("set-cookie")).lower()
    assert "awm_session=" in setc and "httponly" in setc
    assert "samesite=lax" in setc


def test_login_relays_a_lockout_as_429():
    with _client(_app(), token=None) as c:
        r = c.post("/__auth/login", json={"username": "locked", "password": "x"})
    assert r.status_code == 429
    assert r.headers["retry-after"] == "321"
    assert COOKIE_NAME not in r.cookies


def test_bad_login_is_401_and_sets_nothing():
    with _client(_app(), token=None) as c:
        r = c.post("/__auth/login", json={"username": "tony", "password": "no"})
    assert r.status_code == 401 and not r.cookies


def test_logout_clears_both_cookies():
    with _client(_app()) as c:
        r = c.post("/__auth/logout")
    setc = "\n".join(r.headers.get_list("set-cookie"))
    assert "awm_session=" in setc and "awm_as=" in setc


# --- the public profile ------------------------------------------------------

DENIED = [
    "/hub/services", "/invoke", "/tools", "/svc/scopes/fn/scope_search",
    "/svc/auth/fn/password", "/svc/auth/fn/verify", "/svc/notes/fn/purge",
    "/svc/notes/fn/read", "/svc/drawio/fn/export", "/svc/drawio/fn/autopublish_status",
    "/drawio-app/view/x.svg", "/__auth/link", "/ca.crt", "/ca.pem",
    "/ui/agents/", "/files/", "/files/projects/awm/dev/x",
    "/__landing/tags", "/svc/notes/session/x",
]
ALLOWED = [
    "/ui/notes/", "/ui/notes/assets/x.js", "/ui/drawio/", "/drawio-app/index.html",
    "/svc/notes/fn/list", "/svc/notes/fn/collab_open", "/svc/drawio/fn/save",
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
        r = c.get("/ui/notes/", headers={"Accept": "text/html"})
        assert r.status_code == 200 and "username" in r.text
        assert "ca.crt" not in r.text
        assert c.get("/svc/notes/fn/list").status_code == 401


def test_user_scoped_paths_admit_only_their_user():
    with _client(_app("public")) as c:
        _capture(c)
        assert c.get("/files/projects/userdata/tony/notes/a.md").status_code == 200
        assert c.get("/files/projects/userdata/steven/notes/a.md").status_code == 404
        assert c.get("/files/projects/userdata/tony").status_code == 200


def test_emit_topics_must_carry_the_users_prefix():
    assert policy.allows("/svc/notes/emit/note:tony:abc", "tony")
    assert not policy.allows("/svc/notes/emit/note:steven:abc", "tony")
    assert not policy.allows("/svc/notes/emit/note:abc", "tony")
    assert policy.allows("/svc/drawio/emit/drawio:tony:x.drawio", "tony")
    assert not policy.allows("/svc/drawio/emit/drawio:tony:x", None)


def test_root_redirects_to_notes_or_asks_for_login():
    with _client(_app("public")) as c:
        r = c.get("/")
        assert r.status_code == 302 and r.headers["location"] == "/ui/notes/"
    with _client(_app("public"), token=None) as c:
        r = c.get("/", headers={"Accept": "text/html"})
        assert r.status_code == 200 and "username" in r.text


def test_public_cookies_are_samesite_strict():
    with _client(_app("public"), token=None) as c:
        r = c.post("/__auth/login", json={"username": "tony", "password": "pw"})
    setc = "\n".join(r.headers.get_list("set-cookie")).lower()
    assert setc.count("samesite=strict") == 2


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

    monkeypatch.setattr(proxy.uvicorn, "Config", _Cfg)
    monkeypatch.setattr(proxy.uvicorn, "Server", _Srv)
    proxy.serve(port=8444, cert="", key="", ca="", upstream="http://127.0.0.1:1/",
                profile="public", tls=False)
    assert captured["host"] == "127.0.0.1"
    assert captured["proxy_headers"] is True
    assert captured["forwarded_allow_ips"] == "127.0.0.1"
    assert "ssl_certfile" not in captured
    assert captured["log_level"] == "warning"
