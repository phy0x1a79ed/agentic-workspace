"""Integration tests: hub forwards HTTP + WS to a real upstream service.

Ported from the monolith. Federation/auth is retired — the gateway proxy
no longer injects ``X-Awm-From`` or a hub bearer; it strips the client's
``authorization`` + ``cookie`` and passes everything else (including
``X-Awm-As``) through verbatim. The forwarded-header assertions below
reflect that current contract.
"""

from __future__ import annotations


import pytest
pytestmark = [pytest.mark.hub, pytest.mark.slow]

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
        # Surface a few headers so we can assert proxy wiring.
        return {
            "echoed": body.decode(),
            "x_awm_from": req.headers.get("x-awm-from"),
            "x_awm_as": req.headers.get("x-awm-as"),
            "authorization": req.headers.get("authorization"),
            "cookie_seen": req.headers.get("cookie"),
            "transfer_encoding": req.headers.get("transfer-encoding"),
            "content_length": req.headers.get("content-length"),
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
# Hub app fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _clean_registry():
    """Reset the singleton Registry between tests by replacing the
    instance entirely. Avoids cross-loop Lock state and lets each test
    start from a guaranteed-empty registry."""
    from awm.gateway.hub import registry as _reg_mod
    _reg_mod._singleton = _reg_mod.Registry()
    yield
    _reg_mod._singleton = _reg_mod.Registry()


@pytest.fixture()
def exposed_workspace(awm_workspace, monkeypatch):
    awm_dir = awm_workspace["awm_dir"]
    monkeypatch.setattr("awm.config.ACCESS_LOG", awm_dir / "access.log")
    return awm_workspace


@pytest.fixture()
def hub_client(exposed_workspace):
    from awm.gateway.server import app
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


# ---------------------------------------------------------------------------
# HTTP routing
# ---------------------------------------------------------------------------

class TestHttpForwarding:
    def test_post_body_forwarded_and_auth_stripped(self, hub_client):
        with _running_upstream() as port:
            r = hub_client.post(
                "/hub/register",
                json={"name": "demo", "prefix": "/demo",
                      "url": f"http://127.0.0.1:{port}"},
            )
            assert r.status_code == 200, r.text

            r = hub_client.post(
                "/demo/echo",
                content=b"hello world",
                headers={
                    "Authorization": "Bearer client-token",
                    "X-Awm-As": "user:operator",
                    "Cookie": "awm_session=should-be-stripped",
                },
            )
            assert r.status_code == 200, r.text
            data = r.json()
            assert data["echoed"] == "hello world"
            # Client bearer + cookie stripped; no hub bearer/peer injected
            # (federation/auth retired). X-Awm-As preserved.
            assert data["authorization"] is None
            assert data["cookie_seen"] is None
            assert data["x_awm_from"] is None
            assert data["x_awm_as"] == "user:operator"

    def test_client_chunked_body_stays_chunked(self, hub_client):
        """A client that framed its own body as chunked keeps a chunked body
        upstream. The hub strips ``transfer-encoding`` as hop-by-hop and lets
        httpx re-derive framing from the stream, which has no length — so the
        header is regenerated rather than forwarded. See
        ``proxy._forwards_body``: sized uploads must not be converted to
        chunked, and chunked uploads must not lose their framing."""
        def _chunks():
            yield b"hello "
            yield b"world"

        with _running_upstream() as port:
            hub_client.post(
                "/hub/register",
                json={"name": "demo", "prefix": "/demo",
                      "url": f"http://127.0.0.1:{port}"},
            )
            r = hub_client.post("/demo/echo", content=_chunks())
            assert r.status_code == 200, r.text
            data = r.json()
            assert data["echoed"] == "hello world"
            assert data["transfer_encoding"] == "chunked"
            assert data["content_length"] is None

    def test_sized_body_stays_sized(self, hub_client):
        with _running_upstream() as port:
            hub_client.post(
                "/hub/register",
                json={"name": "demo", "prefix": "/demo",
                      "url": f"http://127.0.0.1:{port}"},
            )
            r = hub_client.post("/demo/echo", content=b"hello world")
            assert r.status_code == 200, r.text
            data = r.json()
            assert data["content_length"] == "11"
            assert data["transfer_encoding"] is None

    def test_unregistered_path_falls_through(self, hub_client):
        # /status remains served by the in-process router with /demo
        # registered.
        with _running_upstream() as port:
            hub_client.post(
                "/hub/register",
                json={"name": "demo", "prefix": "/demo",
                      "url": f"http://127.0.0.1:{port}"},
            )
            r = hub_client.get("/status")
            assert r.status_code == 200
            assert r.json()["status"] == "ok"

    def test_query_string_preserved(self, hub_client):
        with _running_upstream() as port:
            hub_client.post(
                "/hub/register",
                json={"name": "demo", "prefix": "/demo",
                      "url": f"http://127.0.0.1:{port}"},
            )
            r = hub_client.get("/demo/path/widget?a=1&b=2")
            assert r.status_code == 200, r.text
            data = r.json()
            assert data["name"] == "widget"
            assert data["query"] == {"a": "1", "b": "2"}

    def test_prefix_conflict_409(self, hub_client):
        with _running_upstream() as port:
            r = hub_client.post(
                "/hub/register",
                json={"name": "a", "prefix": "/x",
                      "url": f"http://127.0.0.1:{port}"},
            )
            assert r.status_code == 200, r.text
            r = hub_client.post(
                "/hub/register",
                json={"name": "b", "prefix": "/x",
                      "url": f"http://127.0.0.1:{port}"},
            )
            assert r.status_code == 409

    def test_deregister_evicts(self, hub_client):
        with _running_upstream() as port:
            hub_client.post(
                "/hub/register",
                json={"name": "demo", "prefix": "/demo",
                      "url": f"http://127.0.0.1:{port}"},
            )
            r = hub_client.delete("/hub/services/demo")
            assert r.status_code == 200
            # After eviction the path falls through; in-process /demo
            # doesn't exist so it's a 404 from FastAPI (not a proxy 502).
            r = hub_client.get("/demo/echo")
            assert r.status_code in (404, 405)


# ---------------------------------------------------------------------------
# WebSocket forwarding
# ---------------------------------------------------------------------------

class TestWebSocketForwarding:
    """WS routing has to be driven against a real hub listener — the
    TestClient WS path can't be intercepted by raw-ASGI middleware in a
    way that lets us bridge to a separately-running upstream. Run the
    app via uvicorn on a free port (plain HTTP, no TLS) and drive both
    halves with the websockets client."""

    def test_ws_echo_round_trip(self, exposed_workspace):
        from awm.gateway.server import app
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
                    timeout=5,
                )
                assert r.status_code == 200, r.text

                ws_url = f"ws://127.0.0.1:{hub_port}/demo/ws"

                async def _exchange():
                    async with websockets.connect(
                        ws_url,
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
