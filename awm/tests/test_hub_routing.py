"""Integration tests: hub forwards HTTP + WS to a real upstream service."""

from __future__ import annotations

import asyncio
import contextlib
import socket
import threading
import time

import httpx
import pytest
import uvicorn
import websockets
from fastapi import FastAPI, Request, WebSocket
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# Upstream echo svc, run on a real port so hub's httpx/websockets clients
# can reach it. The TestClient transport can't proxy to itself.
# ---------------------------------------------------------------------------

def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _build_echo_app() -> FastAPI:
    app = FastAPI()

    @app.post("/demo/echo")
    async def echo(req: Request):
        body = await req.body()
        # Surface a few headers so we can assert auth wiring.
        return {
            "echoed": body.decode(),
            "x_awm_from": req.headers.get("x-awm-from"),
            "x_awm_as": req.headers.get("x-awm-as"),
            "authorization": req.headers.get("authorization"),
            "cookie_seen": req.headers.get("cookie"),
        }

    @app.get("/demo/path/{name}")
    async def path_echo(name: str, req: Request):
        return {"name": name, "query": dict(req.query_params)}

    @app.websocket("/demo/ws")
    async def ws(ws: WebSocket):
        await ws.accept()
        async for msg in ws.iter_text():
            await ws.send_text(f"echo:{msg}")

    return app


@contextlib.contextmanager
def _running_upstream():
    port = _free_port()
    cfg = uvicorn.Config(
        _build_echo_app(), host="127.0.0.1", port=port,
        log_level="error", lifespan="off",
    )
    server = uvicorn.Server(cfg)
    th = threading.Thread(target=server.run, daemon=True)
    th.start()
    # Wait for the socket to accept.
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                break
        except OSError:
            time.sleep(0.05)
    else:
        raise RuntimeError("upstream svc did not start")
    try:
        yield port
    finally:
        server.should_exit = True
        th.join(timeout=2)


# ---------------------------------------------------------------------------
# Hub app fixtures (mirrors test_hub_passthrough.py)
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _clean_registry():
    """Reset the singleton Registry between tests by replacing the
    instance entirely. Avoids cross-loop Lock state and lets each test
    start from a guaranteed-empty registry."""
    from awm.services.hub import registry as _reg_mod
    _reg_mod._singleton = _reg_mod.Registry()
    yield
    _reg_mod._singleton = _reg_mod.Registry()


@pytest.fixture()
def exposed_workspace(awm_workspace, monkeypatch):
    awm_dir = awm_workspace["awm_dir"]
    token_file = awm_dir / "auth.token"
    monkeypatch.setattr("awm.config.AUTH_TOKEN_FILE", token_file)
    monkeypatch.setattr("awm.config.ACCESS_LOG", awm_dir / "access.log")
    monkeypatch.setattr("awm.config.EXPOSED_PID_FILE", awm_dir / "awm-exposed.pid")
    monkeypatch.setattr("awm.config.EXPOSED_LOG_FILE", awm_dir / "awm-exposed.log")
    from awm.services import auth as _auth
    _auth._token_cache.update({"value": None, "mtime": None})
    monkeypatch.delenv("AWM_AUTH_TOKEN", raising=False)
    return {**awm_workspace, "token_file": token_file}


@pytest.fixture()
def good_token(exposed_workspace):
    token = "hub-routing-token"
    exposed_workspace["token_file"].write_text(token + "\n")
    return token


@pytest.fixture()
def hub_client(exposed_workspace, good_token, monkeypatch):
    # The hub injects X-Awm-From from _local_peer_id(). Stub it so we
    # don't need a real peer.json.
    monkeypatch.setattr(
        "awm.services.hub.proxy._local_peer_id", lambda: "test-self-peer",
    )
    from awm.exposed import app
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


# ---------------------------------------------------------------------------
# HTTP routing
# ---------------------------------------------------------------------------

class TestHttpForwarding:
    def test_post_body_forwarded_with_hub_auth(self, hub_client, good_token):
        with _running_upstream() as port:
            r = hub_client.post(
                "/hub/register",
                json={"name": "demo", "prefix": "/demo",
                      "url": f"http://127.0.0.1:{port}"},
                headers={"Authorization": f"Bearer {good_token}"},
            )
            assert r.status_code == 200, r.text

            r = hub_client.post(
                "/demo/echo",
                content=b"hello world",
                headers={
                    "Authorization": f"Bearer {good_token}",
                    "X-Awm-As": "user:operator",
                    "Cookie": "awm_session=should-be-stripped",
                },
            )
            assert r.status_code == 200, r.text
            data = r.json()
            assert data["echoed"] == "hello world"
            # Client bearer stripped, hub bearer injected.
            assert data["authorization"] == f"Bearer {good_token}"
            assert data["x_awm_from"] == "test-self-peer"
            assert data["x_awm_as"] == "user:operator"
            assert data["cookie_seen"] is None

    def test_unregistered_path_falls_through(self, hub_client, good_token):
        # /status remains served by the in-process router with /demo
        # registered.
        with _running_upstream() as port:
            hub_client.post(
                "/hub/register",
                json={"name": "demo", "prefix": "/demo",
                      "url": f"http://127.0.0.1:{port}"},
                headers={"Authorization": f"Bearer {good_token}"},
            )
            r = hub_client.get(
                "/status", headers={"Authorization": f"Bearer {good_token}"},
            )
            assert r.status_code == 200
            assert r.json()["status"] == "ok"

    def test_query_string_preserved(self, hub_client, good_token):
        with _running_upstream() as port:
            hub_client.post(
                "/hub/register",
                json={"name": "demo", "prefix": "/demo",
                      "url": f"http://127.0.0.1:{port}"},
                headers={"Authorization": f"Bearer {good_token}"},
            )
            r = hub_client.get(
                "/demo/path/widget?a=1&b=2",
                headers={"Authorization": f"Bearer {good_token}"},
            )
            assert r.status_code == 200, r.text
            data = r.json()
            assert data["name"] == "widget"
            assert data["query"] == {"a": "1", "b": "2"}

    def test_prefix_conflict_409(self, hub_client, good_token):
        with _running_upstream() as port:
            r = hub_client.post(
                "/hub/register",
                json={"name": "a", "prefix": "/x",
                      "url": f"http://127.0.0.1:{port}"},
                headers={"Authorization": f"Bearer {good_token}"},
            )
            assert r.status_code == 200, r.text
            r = hub_client.post(
                "/hub/register",
                json={"name": "b", "prefix": "/x",
                      "url": f"http://127.0.0.1:{port}"},
                headers={"Authorization": f"Bearer {good_token}"},
            )
            assert r.status_code == 409

    def test_deregister_evicts(self, hub_client, good_token):
        with _running_upstream() as port:
            hub_client.post(
                "/hub/register",
                json={"name": "demo", "prefix": "/demo",
                      "url": f"http://127.0.0.1:{port}"},
                headers={"Authorization": f"Bearer {good_token}"},
            )
            r = hub_client.delete(
                "/hub/services/demo",
                headers={"Authorization": f"Bearer {good_token}"},
            )
            assert r.status_code == 200
            # After eviction the path falls through; in-process /demo
            # doesn't exist so it's a 404 from FastAPI (not a proxy 502).
            r = hub_client.get(
                "/demo/echo",
                headers={"Authorization": f"Bearer {good_token}"},
            )
            assert r.status_code in (404, 405)


# ---------------------------------------------------------------------------
# WebSocket forwarding
# ---------------------------------------------------------------------------

class TestWebSocketForwarding:
    """WS routing has to be driven against a real hub listener — the
    TestClient WS path can't be intercepted by raw-ASGI middleware in a
    way that lets us bridge to a separately-running upstream. Run the
    exposed app via uvicorn on a free port (plain HTTP, no TLS) and
    drive both halves with the websockets client."""

    def test_ws_echo_round_trip(self, exposed_workspace, good_token, monkeypatch):
        monkeypatch.setattr(
            "awm.services.hub.proxy._local_peer_id", lambda: "test-self-peer",
        )
        from awm.exposed import app
        hub_port = _free_port()
        cfg = uvicorn.Config(
            app, host="127.0.0.1", port=hub_port,
            log_level="error", lifespan="off",
        )
        server = uvicorn.Server(cfg)
        th = threading.Thread(target=server.run, daemon=True)
        th.start()
        try:
            _wait_listening(hub_port)
            with _running_upstream() as upstream_port:
                # Register via the live hub.
                r = httpx.post(
                    f"http://127.0.0.1:{hub_port}/hub/register",
                    json={"name": "demo", "prefix": "/demo",
                          "url": f"http://127.0.0.1:{upstream_port}"},
                    headers={"Authorization": f"Bearer {good_token}"},
                    timeout=5,
                )
                assert r.status_code == 200, r.text

                ws_url = f"ws://127.0.0.1:{hub_port}/demo/ws"

                async def _exchange():
                    async with websockets.connect(
                        ws_url,
                        subprotocols=[f"bearer.{good_token}"],
                        max_size=None,
                        open_timeout=5,
                    ) as wsc:
                        await wsc.send("ping")
                        return await asyncio.wait_for(wsc.recv(), timeout=5)

                result = asyncio.new_event_loop().run_until_complete(
                    _exchange(),
                )
                assert result == "echo:ping"
        finally:
            server.should_exit = True
            th.join(timeout=2)


def _wait_listening(port: int, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.05)
    raise RuntimeError(f"port {port} not listening within {timeout}s")
