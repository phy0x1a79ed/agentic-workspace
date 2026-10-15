"""``kind="url"`` registrations that opt into prefix stripping.

A url mount normally forwards the request path verbatim, which suits an
upstream that already serves under the same prefix. ``strip_prefix`` is for
the other shape: an upstream serving at the root that rebases its own links
from ``X-Forwarded-Prefix``. The flag couples both halves — peel the prefix
off the forwarded path, announce it in the header — and is off by default so
every existing mount is untouched.
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
import uvicorn
import websockets
from fastapi import FastAPI, Request, WebSocket
from fastapi.testclient import TestClient


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _wait_listening(port: int, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.05)
    raise RuntimeError(f"port {port} not listening within {timeout}s")


def _build_root_app() -> FastAPI:
    """An upstream that serves at the root — the shape strip_prefix exists for."""
    app = FastAPI()

    @app.get("/")
    async def index(req: Request):
        return {"path": req.url.path,
                "fwd_prefix": req.headers.get("x-forwarded-prefix")}

    @app.get("/api/status")
    async def status(req: Request):
        return {"path": req.url.path,
                "fwd_prefix": req.headers.get("x-forwarded-prefix"),
                "query": dict(req.query_params)}

    @app.websocket("/api/ws")
    async def ws(sock: WebSocket):
        await sock.accept()
        token = sock.query_params.get("token")
        async for msg in sock.iter_text():
            await sock.send_text(f"{token}:{msg}")

    return app


@contextlib.contextmanager
def _running_upstream():
    port = _free_port()
    cfg = uvicorn.Config(_build_root_app(), host="127.0.0.1", port=port,
                         log_level="error", lifespan="off")
    server = uvicorn.Server(cfg)
    th = threading.Thread(target=server.run, daemon=True)
    th.start()
    try:
        _wait_listening(port)
        yield port
    finally:
        server.should_exit = True
        th.join(timeout=2)


@pytest.fixture(autouse=True)
def _clean_registry():
    from awm.gateway.hub import registry as _reg_mod
    _reg_mod._singleton = _reg_mod.Registry()
    yield
    _reg_mod._singleton = _reg_mod.Registry()


@pytest.fixture()
def exposed_workspace(awm_workspace, monkeypatch):
    monkeypatch.setattr("awm.config.ACCESS_LOG",
                        awm_workspace["awm_dir"] / "access.log")
    return awm_workspace


@pytest.fixture()
def hub_client(exposed_workspace):
    from awm.gateway.server import app
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


class TestStripPrefixHttp:
    def test_path_rebased_and_header_sent(self, hub_client):
        with _running_upstream() as port:
            r = hub_client.post(
                "/hub/register",
                json={"name": "rooted", "prefix": "/ui/rooted",
                      "url": f"http://127.0.0.1:{port}", "strip_prefix": True},
            )
            assert r.status_code == 200, r.text
            assert r.json()["strip_prefix"] is True

            r = hub_client.get("/ui/rooted/api/status?a=1")
            assert r.status_code == 200, r.text
            data = r.json()
            assert data["path"] == "/api/status"
            assert data["fwd_prefix"] == "/ui/rooted"
            assert data["query"] == {"a": "1"}

    def test_bare_mount_point_maps_to_root(self, hub_client):
        with _running_upstream() as port:
            hub_client.post(
                "/hub/register",
                json={"name": "rooted", "prefix": "/ui/rooted",
                      "url": f"http://127.0.0.1:{port}", "strip_prefix": True},
            )
            for path in ("/ui/rooted", "/ui/rooted/"):
                r = hub_client.get(path)
                assert r.status_code == 200, (path, r.text)
                assert r.json()["path"] == "/", path

    def test_unflagged_registration_forwards_verbatim(self, hub_client):
        """The default. The upstream sees the mount path and no header."""
        with _running_upstream() as port:
            hub_client.post(
                "/hub/register",
                json={"name": "rooted", "prefix": "/api",
                      "url": f"http://127.0.0.1:{port}"},
            )
            r = hub_client.get("/api/status")
            assert r.status_code == 200, r.text
            data = r.json()
            assert data["path"] == "/api/status"
            assert data["fwd_prefix"] is None

    def test_strip_prefix_rejected_without_url(self, hub_client, tmp_path):
        r = hub_client.post(
            "/hub/register",
            json={"name": "p", "prefix": "/ui/p",
                  "page": {"dir": str(tmp_path)}, "strip_prefix": True},
        )
        assert r.status_code == 422, r.text


class TestStripPrefixWebSocket:
    """WS forwarding needs a real hub listener (see test_hub_routing.py)."""

    def test_ws_path_rebased_query_preserved(self, exposed_workspace):
        from awm.gateway.server import app
        hub_port = _free_port()
        cfg = uvicorn.Config(app, host="127.0.0.1", port=hub_port,
                             log_level="error", lifespan="off")
        server = uvicorn.Server(cfg)
        th = threading.Thread(target=server.run, daemon=True)
        th.start()
        try:
            _wait_listening(hub_port)
            with _running_upstream() as upstream_port:
                r = httpx.post(
                    f"http://127.0.0.1:{hub_port}/hub/register",
                    json={"name": "rooted", "prefix": "/ui/rooted",
                          "url": f"http://127.0.0.1:{upstream_port}",
                          "strip_prefix": True},
                    timeout=5,
                )
                assert r.status_code == 200, r.text

                async def _exchange():
                    url = (f"ws://127.0.0.1:{hub_port}"
                           f"/ui/rooted/api/ws?token=abc")
                    async with websockets.connect(url, max_size=None,
                                                  open_timeout=5) as wsc:
                        await wsc.send("ping")
                        return await asyncio.wait_for(wsc.recv(), timeout=5)

                # The upstream only serves /api/ws; reaching it at all proves
                # the rebase, and the echoed token proves the query survived.
                result = asyncio.new_event_loop().run_until_complete(_exchange())
                assert result == "abc:ping"
        finally:
            server.should_exit = True
            th.join(timeout=2)
