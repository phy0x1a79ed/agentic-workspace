"""The relay reached through the edge, on both legs, with no session at all.

What these pin: the mount is answered before authentication; the path reaches
the relay with the mount taken off and nothing else changed; the operator's
bearer rides through untouched because the relay is what checks it; the edge
asserts no identity it did not verify; the session socket routes to the relay
rather than to the gateway; and a relay that is not configured leaves every one
of these paths as ordinary gateway traffic.
"""

from __future__ import annotations

from contextlib import contextmanager

import httpx
import pytest
from starlette.testclient import TestClient

from awm.httpsfront import proxy

pytestmark = [pytest.mark.unit, pytest.mark.smoke]

GATEWAY = "http://127.0.0.1:7819"
RELAY = "http://127.0.0.1:12520"

TICKET = "0123456789abcdef0123456789abcdef"


class _Gate:
    """No session is ever presented, so nothing here should be consulted."""

    async def authenticate(self, *, cookie=None, bearer=None):
        return False, None, None

    async def session_ttl_seconds(self) -> float:
        return 3600.0

    async def verify_password(self, password: str):
        return None


async def _body():
    yield b"#!/usr/bin/env bash\n"


def _app(*, profile="public", relay=RELAY):
    app = proxy.build_app(GATEWAY + "/", "/dev/null", profile=profile,
                          tether_upstream=relay)
    app.state.gate = _Gate()
    return app


def _recorder():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["method"] = request.method
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


# -- admitted without a session -----------------------------------------------

def test_the_launcher_is_served_to_somebody_with_no_awm_account():
    seen, handler = _recorder()
    with _client(_app(), handler) as c:
        r = c.get("/tether")
    assert r.status_code == 200
    # The relay answers the launcher at its root, which is what makes the
    # address a person is read out — the mount itself — a route at all.
    assert seen["url"] == RELAY + "/"


def test_the_owners_claim_reaches_the_relay_as_a_post():
    seen, handler = _recorder()
    with _client(_app(), handler) as c:
        r = c.post("/tether/claim/7")
    assert r.status_code == 200
    assert seen["method"] == "POST"
    assert seen["url"] == RELAY + "/claim/7"


def test_a_client_download_reaches_the_relay():
    seen, handler = _recorder()
    with _client(_app(), handler) as c:
        c.get("/tether/bin/tether-macos-arm64")
    assert seen["url"] == RELAY + "/bin/tether-macos-arm64"


# -- what the relay is told ----------------------------------------------------

def test_the_operators_bearer_rides_through_for_the_relay_to_check():
    """The edge deliberately does not read it. It is a credential for the
    upstream, not for awm, and an edge that checked it would be a second place
    to get that check wrong."""
    seen, handler = _recorder()
    with _client(_app(), handler) as c:
        c.post("/tether/issue", headers={"Authorization": "Bearer the-issue-token"})
    assert seen["url"] == RELAY + "/issue"
    assert seen["headers"]["authorization"] == "Bearer the-issue-token"


def test_the_edge_asserts_no_identity_it_did_not_verify():
    """Every other leg stamps a verified subject. This one has none, and
    sending the default would be the edge claiming something it never checked."""
    seen, handler = _recorder()
    with _client(_app(), handler) as c:
        c.post("/tether/claim/7", headers={"X-Awm-As": "user:tony"})
    assert "x-awm-as" not in seen["headers"]


def test_the_callers_address_reaches_the_relay_on_the_plain_leg():
    """The only leg where it can. The edge forwards no client address across a
    WebSocket upgrade, so the relay's per-address budget is counted on this
    request or on nothing — which is why the owner's client claims a ticket
    before it opens a socket."""
    seen, handler = _recorder()
    with _client(_app(), handler) as c:
        c.post("/tether/claim/7", headers={"CF-Connecting-IP": "203.0.113.9"})
    assert seen["headers"]["cf-connecting-ip"] == "203.0.113.9"
    assert "x-forwarded-for" in seen["headers"]


# -- refused before it costs anything ------------------------------------------

@pytest.mark.parametrize("path", [
    "/tether/health",
    "/tether/claim/0",
    "/tether/claim/1000",
    f"/tether/join/7/{TICKET[:31]}",
])
def test_a_path_the_mount_does_not_claim_never_reaches_either_upstream(path):
    seen, handler = _recorder()
    with _client(_app(), handler) as c:
        assert c.get(path).status_code == 404
    assert "url" not in seen


def test_a_refused_path_is_a_404_on_a_node_running_no_profile_either():
    """A mesh edge consults no allow-list, so without the mount's own refusal
    the path would reach the ordinary authentication check and answer 401 —
    which says the path exists."""
    seen, handler = _recorder()
    with _client(_app(profile=None), handler) as c:
        assert c.get("/tether/health").status_code == 404
    assert "url" not in seen


def test_a_target_that_re_segments_when_decoded_is_refused():
    seen, handler = _recorder()
    with _client(_app(), handler) as c:
        assert c.get("/tether/bin/%2e%2e/issue").status_code == 404
    assert "url" not in seen


def test_a_relay_that_is_down_is_the_same_404_as_a_slot_that_never_existed():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("nothing listening", request=request)

    with _client(_app(), handler) as c:
        assert c.post("/tether/claim/7").status_code == 404


def test_with_no_relay_configured_the_mount_is_a_404_and_never_a_401():
    """The flag is off by default, and the mount's name is claimed anyway.

    The policy door classifies these paths without consulting any upstream, so
    a path it declares reachable with no session must not then be asked for
    one: a 401 would both contradict the verdict and tell an unauthenticated
    caller that the path is special. Whether a relay is wired decides
    404-versus-proxy, not who owns the name.
    """
    seen, handler = _recorder()
    with _client(_app(relay=None), handler) as c:
        assert c.get("/tether").status_code == 404
        assert c.post("/tether/claim/7").status_code == 404
    assert "url" not in seen


def test_a_socket_with_no_relay_configured_never_reaches_the_gateway(monkeypatch):
    seen = _ws_target(monkeypatch, _app(relay=None), f"/tether/join/7/{TICKET}")
    assert seen == {}


# -- the socket ----------------------------------------------------------------

def _ws_target(monkeypatch, app, path: str) -> dict:
    """Capture the URL ``_ws_proxy`` would dial, then abort the handshake."""
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
            with c.websocket_connect(path):
                pass
        except Exception:  # noqa: BLE001 — the reject is the expected path
            pass
    return captured


def test_the_session_socket_goes_to_the_relay_and_not_to_the_gateway(monkeypatch):
    seen = _ws_target(monkeypatch, _app(), f"/tether/join/7/{TICKET}")
    assert seen["url"] == f"ws://127.0.0.1:12520/join/7/{TICKET}"


def test_the_socket_opens_with_no_session_and_carries_no_identity(monkeypatch):
    seen = _ws_target(monkeypatch, _app(), f"/tether/join/7/{TICKET}")
    assert "x-awm-as" not in seen["headers"]


def test_a_ticket_of_the_wrong_shape_never_becomes_a_socket(monkeypatch):
    seen = _ws_target(monkeypatch, _app(), f"/tether/join/7/{TICKET[:31]}")
    assert seen == {}


def test_a_socket_on_a_path_the_mount_refuses_never_opens(monkeypatch):
    seen = _ws_target(monkeypatch, _app(), "/tether/health")
    assert seen == {}
