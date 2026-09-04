"""A slice of the vault, reached with no awm session at all.

What these pin: the mount is answered before authentication and only for a token
the trilium service confirms; a token it does not confirm is a 404 and never a
403; the path reaches the same child byte for byte; the three headers Trilium
reads are stamped by the edge and cannot be stated by a browser; a bound link
that names the wrong visitor is refused rather than mis-attributed; and an open
link learns its visitor once and keeps the name on its own path.
"""

from __future__ import annotations

from contextlib import contextmanager

import httpx
import pytest
from starlette.testclient import TestClient

from awm.httpsfront import proxy, slices

pytestmark = [pytest.mark.unit, pytest.mark.smoke]

GATEWAY = "http://127.0.0.1:7819"
VAULT = "http://127.0.0.1:12511"

BOUND = "Ab3-_9xYzQw012345678"
OPEN = "Zz9-_0aBcDe987654321"

#: What the trilium service answers for each token — the whole of the contract
#: this edge depends on.
SLICES = {
    BOUND: {"found": True, "note_id": "abc123", "user": "steven", "write": True},
    OPEN: {"found": True, "note_id": "def456", "user": None, "write": True},
}

#: What the service answers for a token it does not know — a shape, never a
#: null, so the edge has to read ``found`` rather than the truth of the body.
UNKNOWN = {"found": False, "note_id": None, "user": None, "write": False}


class _Gate:
    """No session is ever presented, so nothing here should be consulted."""

    async def authenticate(self, *, cookie=None, bearer=None):
        return False, None, None

    async def session_ttl_seconds(self) -> float:
        return 3600.0

    async def verify_password(self, password: str):
        return None


async def _body():
    yield b"ok"


def _app(*, profile="public"):
    app = proxy.build_app(GATEWAY + "/", "/dev/null", profile=profile,
                          vault_upstream=VAULT)
    app.state.gate = _Gate()
    return app


def _recorder(known=None):
    """A transport answering both legs: the gateway's /invoke, then the vault."""
    known = SLICES if known is None else known
    seen: dict = {"resolves": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/invoke":
            seen["resolves"] += 1
            import json
            token = json.loads(request.content)["args"]["token"]
            return httpx.Response(200, json={"result": known.get(token, UNKNOWN)})
        seen["url"] = str(request.url)
        seen["headers"] = {k.lower(): v for k, v in request.headers.items()}
        return httpx.Response(200, content=_body())
    return seen, handler


@contextmanager
def _client(app, handler):
    c = TestClient(app, base_url="https://nexus.example", follow_redirects=False)
    with c:
        c.app.state.client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler))
        yield c


# -- admitted without a session ----------------------------------------------

def test_a_slice_is_served_to_a_browser_that_has_never_signed_in():
    seen, handler = _recorder()
    with _client(_app(), handler) as c:
        r = c.get(f"/slice/{BOUND}/")
    assert r.status_code == 200
    assert seen["url"] == VAULT + "/"


def test_the_path_reaches_the_child_byte_for_byte():
    seen, handler = _recorder()
    with _client(_app(), handler) as c:
        c.get(f"/slice/{BOUND}/api/search/%23book")
    assert seen["url"] == VAULT + "/api/search/%23book"


def test_the_bare_mount_is_a_permanent_redirect_to_the_shell():
    _, handler = _recorder()
    with _client(_app(), handler) as c:
        r = c.get(f"/slice/{BOUND}")
    assert r.status_code == 308
    assert r.headers["location"] == f"/slice/{BOUND}/"


def test_a_token_the_service_does_not_know_is_a_404_not_a_403():
    # A slice that was revoked, expired or never existed all look alike from
    # outside, which is the point.
    _, handler = _recorder(known={})
    with _client(_app(), handler) as c:
        assert c.get(f"/slice/{BOUND}/").status_code == 404
        assert c.get(f"/slice/{BOUND}/api/tree").status_code == 404


def test_a_refused_path_inside_the_mount_never_reaches_the_child():
    seen, handler = _recorder()
    with _client(_app(), handler) as c:
        assert c.get(f"/slice/{BOUND}/etapi/app-info").status_code == 404
    assert "url" not in seen


def test_a_target_that_re_segments_when_decoded_is_refused():
    # The traversal that once let /trilium/api/%2e%2e/etapi/… reach the
    # unauthenticated ETAPI, closed on this mount by the same check.
    seen, handler = _recorder()
    with _client(_app(), handler) as c:
        r = c.get(f"/slice/{BOUND}/api/%2e%2e/etapi/app-info")
    assert r.status_code == 404
    assert "url" not in seen


# -- what the child is told ---------------------------------------------------

def test_the_edge_stamps_the_slice_headers_and_no_identity():
    seen, handler = _recorder()
    with _client(_app(), handler) as c:
        c.get(f"/slice/{BOUND}/api/tree")
    h = seen["headers"]
    assert h[slices.HEADER_ROOT.lower()] == "abc123"
    assert h[slices.HEADER_USER.lower()] == "steven"
    assert h[slices.HEADER_WRITE.lower()] == "1"
    assert "x-awm-as" not in h


def test_a_browser_cannot_state_the_slice_headers_for_itself():
    seen, handler = _recorder()
    with _client(_app(), handler) as c:
        c.get(f"/slice/{BOUND}/api/tree", headers={
            slices.HEADER_ROOT: "root",
            slices.HEADER_USER: "administrator",
            slices.HEADER_WRITE: "1",
            "X-Awm-As": "user:tony",
        })
    h = seen["headers"]
    assert h[slices.HEADER_ROOT.lower()] == "abc123"
    assert h[slices.HEADER_USER.lower()] == "steven"
    assert "x-awm-as" not in h


def test_a_read_only_slice_says_so():
    known = {BOUND: {"found": True, "note_id": "abc123", "user": "steven",
                     "write": False}}
    seen, handler = _recorder(known=known)
    with _client(_app(), handler) as c:
        c.get(f"/slice/{BOUND}/api/tree")
    assert seen["headers"][slices.HEADER_WRITE.lower()] == "0"


# -- who is visiting ----------------------------------------------------------

def test_a_bound_link_that_names_somebody_else_is_refused_loudly():
    seen, handler = _recorder()
    with _client(_app(), handler) as c:
        r = c.get(f"/slice/{BOUND}/?user=mallory")
    assert r.status_code == 400
    assert "url" not in seen


def test_a_bound_link_may_display_the_name_it_was_minted_with():
    seen, handler = _recorder()
    with _client(_app(), handler) as c:
        r = c.get(f"/slice/{BOUND}/?user=steven")
    assert r.status_code == 200
    assert seen["headers"][slices.HEADER_USER.lower()] == "steven"


def test_an_open_link_learns_its_visitor_once_and_keeps_the_name():
    seen, handler = _recorder()
    with _client(_app(), handler) as c:
        r = c.get(f"/slice/{OPEN}/?user=steven")
        assert seen["headers"][slices.HEADER_USER.lower()] == "steven"
        cookie = r.headers["set-cookie"]
        assert slices.cookie_name(OPEN) in cookie
        assert f"Path=/slice/{OPEN}/" in cookie
        # The name survives into the requests the shell makes for itself, which
        # carry no query string of their own.
        c.get(f"/slice/{OPEN}/api/tree")
    assert seen["headers"][slices.HEADER_USER.lower()] == "steven"


def test_an_open_link_with_no_name_yet_is_served_anonymously():
    seen, handler = _recorder()
    with _client(_app(), handler) as c:
        r = c.get(f"/slice/{OPEN}/")
    assert r.status_code == 200
    assert slices.HEADER_USER.lower() not in seen["headers"]
    assert "set-cookie" not in r.headers


def test_a_bound_slice_sets_no_cookie_at_all():
    _, handler = _recorder()
    with _client(_app(), handler) as c:
        r = c.get(f"/slice/{BOUND}/?user=steven")
    assert "set-cookie" not in r.headers


# -- the cost of asking -------------------------------------------------------

def test_a_page_load_resolves_the_token_once_not_once_per_asset():
    seen, handler = _recorder()
    with _client(_app(), handler) as c:
        for _ in range(5):
            c.get(f"/slice/{BOUND}/api/tree")
    assert seen["resolves"] == 1


# -- the socket ---------------------------------------------------------------
#
# Trilium builds its socket URI from ``location.pathname``, so a shell served
# under a slice opens its socket under that slice and nowhere else.

def _ws_target(monkeypatch, app, path: str, *, known=None,
               headers=None) -> dict:
    """Capture the URL ``_ws_proxy`` would dial, then abort the handshake."""
    captured: dict = {}

    async def _fake_connect(url, *, additional_headers=None, **kw):
        captured["url"] = url
        captured["headers"] = {k.lower(): v
                               for k, v in (additional_headers or {}).items()}
        raise RuntimeError("upstream refused")  # closes the handshake cleanly

    monkeypatch.setattr(proxy.websockets, "connect", _fake_connect)
    _, handler = _recorder(known=known)
    with _client(app, handler) as c:
        try:
            with c.websocket_connect(path, headers=headers or {}):
                pass
        except Exception:  # noqa: BLE001 — the reject is the expected path
            pass
    return captured


def test_a_slices_socket_follows_its_shell(monkeypatch):
    seen = _ws_target(monkeypatch, _app(), f"/slice/{BOUND}/")
    assert seen["url"] == "ws://127.0.0.1:12511/"
    assert seen["headers"][slices.HEADER_ROOT.lower()] == "abc123"
    assert seen["headers"][slices.HEADER_USER.lower()] == "steven"
    assert "x-awm-as" not in seen["headers"]


def test_an_unknown_token_never_opens_a_socket(monkeypatch):
    seen = _ws_target(monkeypatch, _app(), f"/slice/{BOUND}/", known={})
    assert seen == {}


def test_an_open_slices_socket_carries_the_name_from_its_cookie(monkeypatch):
    seen = _ws_target(
        monkeypatch, _app(), f"/slice/{OPEN}/",
        headers={"cookie": f"{slices.cookie_name(OPEN)}=steven"})
    assert seen["headers"][slices.HEADER_USER.lower()] == "steven"
