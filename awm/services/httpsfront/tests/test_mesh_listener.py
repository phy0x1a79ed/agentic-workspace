"""The mesh listener: a public node's second door, for peers on the overlay.

What these pin: the listener is off unless ``AWM_EDGE_MESH_HOST`` names an
address; when on, it binds that address alone with TLS and the default profile,
and fronts the gateway with no app door of its own; a peer's cx call is
forwarded on it and is still 404 on the public door beside it.
"""

from __future__ import annotations

import importlib

import httpx
import pytest
from starlette.testclient import TestClient

from awm.httpsfront import proxy

pytestmark = [pytest.mark.unit]

PEER_CALL = "/svc/cx/fn/list"


class _Gate:
    async def authenticate(self, *, cookie=None, bearer=None):
        if bearer == "node-capella":
            return True, None, "peer:capella"
        return False, None, None

    def peer_relation(self, node):
        return {"capella": "domestic"}.get(node)

    async def session_ttl_seconds(self):
        return 3600.0


@pytest.fixture(autouse=True)
def _fresh_adapter():
    yield
    from awm.httpsfront import hub_adapter
    importlib.reload(hub_adapter)


def _adapter(monkeypatch, **env):
    for key in ("AWM_EDGE_MESH_HOST", "AWM_EDGE_MESH_PORT"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    from awm.httpsfront import hub_adapter
    return importlib.reload(hub_adapter)


def test_the_mesh_listener_is_off_by_default(monkeypatch):
    assert _adapter(monkeypatch).mesh_listener() is None


def test_the_mesh_listener_binds_its_address_alone(monkeypatch):
    hub = _adapter(monkeypatch, AWM_EDGE_MESH_HOST="10.74.81.5", AWM_EDGE_TLS="0",
                   AWM_EDGE_PROFILE="public")
    assert hub.mesh_listener() == {"host": "10.74.81.5", "port": 12100,
                                   "tls": True, "profile": None}
    assert hub.PROFILE == "public" and not hub.TLS


def test_the_mesh_port_can_move(monkeypatch):
    hub = _adapter(monkeypatch, AWM_EDGE_MESH_HOST="10.74.81.5", AWM_EDGE_MESH_PORT="12101")
    assert hub.mesh_listener()["port"] == 12101


def test_serve_binds_the_given_host(monkeypatch):
    seen = {}

    class _Server:
        def __init__(self, config):
            seen.update(host=config.host, port=config.port, ssl=config.ssl_certfile)

        def run(self):
            pass

    monkeypatch.setattr(proxy.uvicorn, "Server", _Server)
    proxy.serve(port=12100, cert="c.pem", key="k.pem", ca="ca.pem",
                upstream="http://127.0.0.1:1/", host="10.74.81.5")
    assert seen == {"host": "10.74.81.5", "port": 12100, "ssl": "c.pem"}
    proxy.serve(port=8443, cert="c.pem", key="k.pem", ca="ca.pem",
                upstream="http://127.0.0.1:1/")
    assert seen["host"] == "0.0.0.0"


async def _body():
    yield b"ok"


def _status(profile):
    app = proxy.build_app("http://127.0.0.1:1/", "/nonexistent/ca.pem", profile=profile)
    app.state.gate = _Gate()
    app.state.client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=_body())))
    c = TestClient(app, base_url="https://front.example", follow_redirects=False)
    return c.post(PEER_CALL, json={}, headers={"Authorization": "Bearer node-capella"}).status_code


def test_a_peer_call_is_forwarded_on_the_mesh_and_refused_on_the_public_door():
    assert _status(None) == 200
    assert _status("public") == 404
