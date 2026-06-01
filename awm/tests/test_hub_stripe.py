"""Integration tests: hub kind=stripe registration with supervised backend.

Covers the new mechanism added for the vertical-stripe dev infrastructure:

* RegisterRequest.stripe → registry.register_stripe + supervisor.spawn_backend
* PortPool allocates from AWM_STRIPE_PORT_POOL; ${AWM_SERVICE_PORT}
  substitution in cmd args
* Health gate flips backend_status starting → ready (visible via /hub/stripes)
* <prefix>/_api/* proxies to the supervised backend with path rewriting
* 503 + Retry-After while backend_status != "ready"
* Backendless stripe registers and serves static without spawning
* SIGTERM on lease close releases the port
"""

from __future__ import annotations

import os
import sys
import textwrap
import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# Backend payload: a tiny Python script that serves /healthz and /echo on
# AWM_SERVICE_PORT. Written to a tempfile per test, spawned by the hub.
# ---------------------------------------------------------------------------

_BACKEND_SRC = textwrap.dedent(
    """
    import json, os, sys
    from http.server import BaseHTTPRequestHandler, HTTPServer

    PORT = int(os.environ["AWM_SERVICE_PORT"])

    class H(BaseHTTPRequestHandler):
        def log_message(self, fmt, *a): pass
        def do_GET(self):
            if self.path == "/healthz":
                self.send_response(200); self.send_header("content-type","text/plain"); self.end_headers()
                self.wfile.write(b"ok")
                return
            if self.path.startswith("/echo"):
                msg = self.path.split("=",1)[1] if "=" in self.path else "hi"
                # X-Awm-As echoed so tests can assert the hub minted it.
                body = json.dumps({
                    "echoed": msg,
                    "port": PORT,
                    "xAwmAs": self.headers.get("X-Awm-As"),
                }).encode()
                self.send_response(200); self.send_header("content-type","application/json"); self.end_headers()
                self.wfile.write(body)
                return
            self.send_response(404); self.end_headers()

    HTTPServer(("127.0.0.1", PORT), H).serve_forever()
    """
)


# WS-capable backend (for the WS-injection test). websockets.serve hosts
# both modes: process_request answers the /healthz HTTP poll the
# supervisor uses to mark the stripe ready; handler() runs on a successful
# Upgrade and echoes the X-Awm-As it saw on the handshake.
_BACKEND_SRC_WS = textwrap.dedent(
    """
    import asyncio, json, os
    from websockets.asyncio.server import serve
    from websockets.datastructures import Headers
    from websockets.http11 import Response

    PORT = int(os.environ["AWM_SERVICE_PORT"])

    async def handler(ws):
        x_awm_as = ws.request.headers.get("X-Awm-As")
        await ws.send(json.dumps({"xAwmAs": x_awm_as}))

    def process_request(connection, request):
        if request.path == "/healthz":
            h = Headers()
            h["content-type"] = "text/plain"
            return Response(200, "OK", h, b"ok")
        return None  # everything else proceeds as a WS upgrade attempt

    async def main():
        async with serve(handler, "127.0.0.1", PORT, process_request=process_request):
            await asyncio.Future()

    asyncio.run(main())
    """
)


def _write_backend(tmp_path: Path, port_via_argv: bool = False) -> Path:
    src = _BACKEND_SRC
    if port_via_argv:
        src = src.replace(
            'PORT = int(os.environ["AWM_SERVICE_PORT"])',
            "PORT = int(sys.argv[1])",
        )
    bp = tmp_path / "backend.py"
    bp.write_text(src)
    return bp


def _write_ws_backend(tmp_path: Path) -> Path:
    bp = tmp_path / "ws_backend.py"
    bp.write_text(_BACKEND_SRC_WS)
    return bp


# ---------------------------------------------------------------------------
# Fixtures (shape mirrors test_hub_routing.py)
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _clean_hub_state():
    """Reset registry + port pool + kill any leaked supervised backends
    between tests. Order matters: the supervisor's _reset_for_tests is
    sync (no event-loop dependency) so it works across the per-test
    TestClient loops that have already been torn down.

    Also clear the proxy's per-loop httpx client cache: it's keyed by
    id(loop), and Python recycles object IDs once a TestClient's loop
    is GC'd, so a fresh loop can collide with a stale dead-loop entry
    and the cached AsyncClient would 500 when reused.
    """
    from awm.services.hub import proxy as _proxy
    from awm.services.hub import registry as _reg_mod
    from awm.services.hub import supervisor as _sup
    _sup._reset_for_tests()
    _reg_mod._singleton = _reg_mod.Registry()
    _proxy._http_clients.clear()
    yield
    _sup._reset_for_tests()
    _reg_mod._singleton = _reg_mod.Registry()
    _proxy._http_clients.clear()


@pytest.fixture()
def exposed_workspace(awm_workspace, monkeypatch):
    awm_dir = awm_workspace["awm_dir"]
    token_file = awm_dir / "auth.token"
    monkeypatch.setattr("awm.config.AUTH_TOKEN_FILE", token_file)
    monkeypatch.setattr("awm.config.ACCESS_LOG", awm_dir / "access.log")
    monkeypatch.setattr("awm.config.EXPOSED_PID_FILE", awm_dir / "awm-exposed.pid")
    monkeypatch.setattr("awm.config.EXPOSED_LOG_FILE", awm_dir / "awm-exposed.log")
    monkeypatch.setattr("awm.config.AWM_DIR", awm_dir)
    from awm.services import auth as _auth
    _auth._token_cache.update({"value": None, "mtime": None})
    monkeypatch.delenv("AWM_AUTH_TOKEN", raising=False)
    return {**awm_workspace, "token_file": token_file}


@pytest.fixture()
def good_token(exposed_workspace):
    token = "hub-stripe-token"
    exposed_workspace["token_file"].write_text(token + "\n")
    return token


@pytest.fixture()
def hub_client(exposed_workspace, good_token, monkeypatch):
    monkeypatch.setattr(
        "awm.services.hub.proxy._local_peer_id", lambda: "test-self-peer",
    )
    from awm.exposed import app
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


def _wait_ready(hub_client, token: str, name: str, timeout: float = 10.0) -> dict:
    deadline = time.monotonic() + timeout
    last: dict = {}
    while time.monotonic() < deadline:
        r = hub_client.get(
            "/hub/stripes", headers={"Authorization": f"Bearer {token}"},
        )
        if r.status_code == 200:
            data = r.json()["stripes"]
            last = data.get(name, {})
            if last.get("status") == "ready":
                return last
            if last.get("status") == "down":
                raise AssertionError(f"stripe {name} went down: {last}")
        time.sleep(0.1)
    raise AssertionError(f"stripe {name} never became ready: last={last}")


# ---------------------------------------------------------------------------
# Port pool unit tests
# ---------------------------------------------------------------------------

class TestPortPool:
    def test_allocate_returns_port_in_range(self):
        import asyncio
        from awm.services.hub.supervisor import PortPool
        pool = PortPool(7900, 7910)
        port = asyncio.run(pool.allocate())
        assert 7900 <= port <= 7910

    def test_substitution_replaces_placeholder(self):
        from awm.services.hub.supervisor import _substitute_port
        out = _substitute_port(
            ["python", "backend.py", "${AWM_SERVICE_PORT}", "--host", "127.0.0.1"],
            8042,
        )
        assert out == ["python", "backend.py", "8042", "--host", "127.0.0.1"]

    def test_substitution_no_op_when_placeholder_absent(self):
        from awm.services.hub.supervisor import _substitute_port
        out = _substitute_port(["node", "server.js"], 8042)
        assert out == ["node", "server.js"]


# ---------------------------------------------------------------------------
# End-to-end: register a stripe with a supervised backend
# ---------------------------------------------------------------------------

class TestStripeRegistration:
    def test_register_spawn_health_ready(self, hub_client, good_token, tmp_path):
        backend = _write_backend(tmp_path)
        # Frontend is just an empty dir + a sentinel file so static serving works.
        front = tmp_path / "front"
        front.mkdir()
        (front / "index.html").write_text("<!doctype html>hello stripe")

        r = hub_client.post(
            "/hub/register",
            json={
                "name": "demo-stripe",
                "prefix": "/demo",
                "stripe": {
                    "dir": str(front),
                    "backend": {
                        "cmd": [sys.executable, str(backend)],
                        "health": "/healthz",
                    },
                },
            },
            headers={"Authorization": f"Bearer {good_token}"},
        )
        assert r.status_code == 200, r.text
        assert r.json()["kind"] == "stripe"

        entry = _wait_ready(hub_client, good_token, "demo-stripe")
        assert entry["backend_url"] == "/demo/_api/"
        assert entry["frontend_url"] == "/demo"

    def test_api_subpath_proxies_to_backend(
        self, hub_client, good_token, tmp_path
    ):
        backend = _write_backend(tmp_path)
        front = tmp_path / "front"
        front.mkdir()
        (front / "index.html").write_text("hi")

        r = hub_client.post(
            "/hub/register",
            json={
                "name": "demo-stripe",
                "prefix": "/demo",
                "stripe": {
                    "dir": str(front),
                    "backend": {
                        "cmd": [sys.executable, str(backend)],
                        "health": "/healthz",
                    },
                },
            },
            headers={"Authorization": f"Bearer {good_token}"},
        )
        assert r.status_code == 200, r.text
        _wait_ready(hub_client, good_token, "demo-stripe")

        r = hub_client.get(
            "/demo/_api/echo?msg=alpha",
            headers={"Authorization": f"Bearer {good_token}"},
        )
        assert r.status_code == 200, r.text
        assert r.json()["echoed"] == "alpha"

    def test_frontend_root_served_by_static(
        self, hub_client, good_token, tmp_path
    ):
        backend = _write_backend(tmp_path)
        front = tmp_path / "front"
        front.mkdir()
        (front / "index.html").write_text("<!doctype html>FRONTEND-INDEX")

        hub_client.post(
            "/hub/register",
            json={
                "name": "demo-stripe",
                "prefix": "/demo",
                "stripe": {
                    "dir": str(front),
                    "backend": {
                        "cmd": [sys.executable, str(backend)],
                        "health": "/healthz",
                    },
                },
            },
            headers={"Authorization": f"Bearer {good_token}"},
        )
        _wait_ready(hub_client, good_token, "demo-stripe")

        r = hub_client.get("/demo/")
        assert r.status_code == 200
        assert "FRONTEND-INDEX" in r.text

    def test_port_substitution_in_cmd_argv(
        self, hub_client, good_token, tmp_path
    ):
        backend = _write_backend(tmp_path, port_via_argv=True)
        front = tmp_path / "front"
        front.mkdir()
        (front / "index.html").write_text("hi")

        r = hub_client.post(
            "/hub/register",
            json={
                "name": "demo-argv",
                "prefix": "/demo-argv",
                "stripe": {
                    "dir": str(front),
                    "backend": {
                        "cmd": [
                            sys.executable, str(backend), "${AWM_SERVICE_PORT}",
                        ],
                        "health": "/healthz",
                    },
                },
            },
            headers={"Authorization": f"Bearer {good_token}"},
        )
        assert r.status_code == 200, r.text
        entry = _wait_ready(hub_client, good_token, "demo-argv")
        # The substituted port appears in the echo response.
        r = hub_client.get(
            "/demo-argv/_api/echo?msg=z",
            headers={"Authorization": f"Bearer {good_token}"},
        )
        assert r.status_code == 200, r.text
        # Backend writes its bound port in the response — proves substitution.
        assert isinstance(r.json()["port"], int)

    def test_backendless_stripe_registers_without_spawning(
        self, hub_client, good_token, tmp_path
    ):
        front = tmp_path / "front"
        front.mkdir()
        (front / "index.html").write_text("<!doctype html>FRONT-ONLY")

        r = hub_client.post(
            "/hub/register",
            json={
                "name": "front-only",
                "prefix": "/front-only",
                "stripe": {"dir": str(front)},
            },
            headers={"Authorization": f"Bearer {good_token}"},
        )
        assert r.status_code == 200, r.text

        # /hub/stripes shows it ready immediately (no backend to wait on).
        r = hub_client.get(
            "/hub/stripes", headers={"Authorization": f"Bearer {good_token}"},
        )
        entry = r.json()["stripes"]["front-only"]
        assert entry["status"] == "ready"
        assert entry["backend_url"] is None

        # _api/ is 404 because there is no backend.
        r = hub_client.get(
            "/front-only/_api/whatever",
            headers={"Authorization": f"Bearer {good_token}"},
        )
        assert r.status_code == 404

        # Frontend root served.
        r = hub_client.get("/front-only/")
        assert r.status_code == 200
        assert "FRONT-ONLY" in r.text

    def test_deregister_terminates_backend(
        self, hub_client, good_token, tmp_path
    ):
        backend = _write_backend(tmp_path)
        front = tmp_path / "front"
        front.mkdir()
        (front / "index.html").write_text("hi")

        r = hub_client.post(
            "/hub/register",
            json={
                "name": "demo-stripe",
                "prefix": "/demo",
                "stripe": {
                    "dir": str(front),
                    "backend": {
                        "cmd": [sys.executable, str(backend)],
                        "health": "/healthz",
                    },
                },
            },
            headers={"Authorization": f"Bearer {good_token}"},
        )
        assert r.status_code == 200, r.text
        entry = _wait_ready(hub_client, good_token, "demo-stripe")
        # Snapshot the port so we can confirm it's free post-terminate.
        # /hub/stripes doesn't surface the raw port (proxy path only), so
        # we read it via /hub/services.
        r = hub_client.get(
            "/hub/services", headers={"Authorization": f"Bearer {good_token}"},
        )
        port = None
        for svc in r.json()["services"]:
            if svc["name"] == "demo-stripe":
                port = svc["stripe"]["backend_port"]
        assert port is not None

        r = hub_client.delete(
            "/hub/services/demo-stripe",
            headers={"Authorization": f"Bearer {good_token}"},
        )
        assert r.status_code == 200

        # Give the supervisor a moment to terminate.
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            try:
                with httpx.Client(timeout=0.2) as c:
                    c.get(f"http://127.0.0.1:{port}/healthz")
            except (httpx.HTTPError, OSError):
                break
            time.sleep(0.1)
        else:
            raise AssertionError(
                f"backend on :{port} still answering after deregister"
            )


# ---------------------------------------------------------------------------
# Identity injection: hub mints X-Awm-As from the awm_as cookie on the
# proxy hop, overrides any inbound forge, omits the header when there's
# no cookie. WS path verified separately.
# ---------------------------------------------------------------------------

def _register_demo(hub_client, good_token, tmp_path, backend_path):
    front = tmp_path / "front"
    front.mkdir()
    (front / "index.html").write_text("hi")
    r = hub_client.post(
        "/hub/register",
        json={
            "name": "demo-stripe",
            "prefix": "/demo",
            "stripe": {
                "dir": str(front),
                "backend": {
                    "cmd": [sys.executable, str(backend_path)],
                    "health": "/healthz",
                },
            },
        },
        headers={"Authorization": f"Bearer {good_token}"},
    )
    assert r.status_code == 200, r.text
    _wait_ready(hub_client, good_token, "demo-stripe")


class TestStripeIdentityInjection:
    def test_stripe_proxy_injects_x_awm_as_from_cookie(
        self, hub_client, good_token, tmp_path,
    ):
        backend = _write_backend(tmp_path)
        _register_demo(hub_client, good_token, tmp_path, backend)

        hub_client.cookies.set("awm_as", "alice")
        r = hub_client.get(
            "/demo/_api/echo?msg=hi",
            headers={"Authorization": f"Bearer {good_token}"},
        )
        assert r.status_code == 200, r.text
        assert r.json()["xAwmAs"] == "alice"

    def test_stripe_proxy_overrides_inbound_x_awm_as(
        self, hub_client, good_token, tmp_path,
    ):
        backend = _write_backend(tmp_path)
        _register_demo(hub_client, good_token, tmp_path, backend)

        hub_client.cookies.set("awm_as", "alice")
        r = hub_client.get(
            "/demo/_api/echo?msg=hi",
            headers={
                "Authorization": f"Bearer {good_token}",
                "X-Awm-As": "evil",
            },
        )
        assert r.status_code == 200, r.text
        # Cookie wins; the inbound forge was stripped before _hub_headers
        # appended the cookie-derived value.
        assert r.json()["xAwmAs"] == "alice"

    def test_stripe_proxy_omits_x_awm_as_when_no_cookie(
        self, hub_client, good_token, tmp_path,
    ):
        backend = _write_backend(tmp_path)
        _register_demo(hub_client, good_token, tmp_path, backend)

        # Make sure the test client doesn't carry a leftover cookie.
        hub_client.cookies.clear()
        r = hub_client.get(
            "/demo/_api/echo?msg=hi",
            headers={"Authorization": f"Bearer {good_token}"},
        )
        assert r.status_code == 200, r.text
        # No cookie → no X-Awm-As header at all → BaseHTTPRequestHandler
        # returns None for an absent header.
        assert r.json()["xAwmAs"] is None

    def test_stripe_proxy_ws_injects_x_awm_as_from_cookie(
        self, hub_client, good_token, tmp_path,
    ):
        backend = _write_ws_backend(tmp_path)
        front = tmp_path / "front"
        front.mkdir()
        (front / "index.html").write_text("hi")
        r = hub_client.post(
            "/hub/register",
            json={
                "name": "ws-stripe",
                "prefix": "/ws-demo",
                "stripe": {
                    "dir": str(front),
                    "backend": {
                        "cmd": [sys.executable, str(backend)],
                        "health": "/healthz",
                    },
                },
            },
            headers={"Authorization": f"Bearer {good_token}"},
        )
        assert r.status_code == 200, r.text
        _wait_ready(hub_client, good_token, "ws-stripe")

        hub_client.cookies.set("awm_as", "alice")
        # The WS backend captures X-Awm-As from the upstream Upgrade
        # headers and echoes it as the first frame. Receiving "alice"
        # proves the hub minted the header on the Upgrade hop.
        with hub_client.websocket_connect(
            "/ws-demo/_api/ws",
            headers={"Authorization": f"Bearer {good_token}"},
            subprotocols=[f"bearer.{good_token}"],
        ) as ws:
            first = ws.receive_text()
        import json as _json
        assert _json.loads(first)["xAwmAs"] == "alice"
