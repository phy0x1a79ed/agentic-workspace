"""End-to-end tests for the kind="service" dispatch path.

Spins up the real ``awm.exposed:app`` on a free port and exercises the
HTTP + WS surface that browsers see:

* ``POST /svc/X/fn/<name>``   → ``call`` / ``notify`` on the control WS.
* ``POST /svc/X/session/<k>`` → ``session.open`` handshake.
* ``WS   /svc/X/session/<id>`` → direct bridge byte-relay.
* ``WS   /svc/X/emit/<topic>`` → ``sub``/``emit`` fan-out.

A "fake service" coroutine holds the control WS, optionally opens a bridge
WS on demand, and reacts to control envelopes by enqueuing replies — so
the entire RPC round-trip runs against the real translator code in
proxy.py + the real envelope state machine in rpc.py.
"""

from __future__ import annotations

import pytest
pytestmark = [pytest.mark.hub, pytest.mark.slow]

import asyncio
import json
import socket
import threading
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest
import uvicorn
import websockets
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# Shared fixtures (mirror test_hub_routing.py)
# ---------------------------------------------------------------------------


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


@pytest.fixture(autouse=True)
def _clean_registry_and_rpc():
    from awm.services.hub import registry as reg_mod
    from awm.services.hub import rpc as rpc_mod
    reg_mod._singleton = reg_mod.Registry()
    rpc_mod._channels.clear()
    yield
    reg_mod._singleton = reg_mod.Registry()
    rpc_mod._channels.clear()


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
    token = "svc-dispatch-token"
    exposed_workspace["token_file"].write_text(token + "\n")
    return token


@pytest.fixture()
def running_hub(exposed_workspace, good_token, monkeypatch):
    """Start the real awm.exposed app on a free port; tear down on exit."""
    monkeypatch.setattr(
        "awm.services.hub.proxy._local_peer_id", lambda: "test-self-peer",
    )
    from awm.exposed import app
    port = _free_port()
    cfg = uvicorn.Config(
        app, host="127.0.0.1", port=port,
        log_level="error", lifespan="off",
    )
    server = uvicorn.Server(cfg)
    th = threading.Thread(target=server.run, daemon=True)
    th.start()
    try:
        _wait_listening(port)
        yield {"port": port, "token": good_token}
    finally:
        server.should_exit = True
        th.join(timeout=2)


# ---------------------------------------------------------------------------
# Fake-service runner: holds the control WS, optionally accepts bridges.
# ---------------------------------------------------------------------------


class FakeService:
    """Connects as a service to /hub/service/control/{sid}, parses
    envelopes, lets each test script its reactions per kind."""

    def __init__(self, host_port: int, token: str, name: str,
                 api: dict[str, Any]):
        self.host_port = host_port
        self.token = token
        self.name = name
        self.api = api
        self.received: list[dict[str, Any]] = []
        self._send_q: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._service_id: str | None = None
        self._control_ws = None
        self._reader_task: asyncio.Task | None = None
        self._writer_task: asyncio.Task | None = None

    @property
    def service_id(self) -> str:
        assert self._service_id is not None
        return self._service_id

    async def register(self) -> str:
        async with httpx.AsyncClient(timeout=5) as client:
            r = await client.post(
                f"http://127.0.0.1:{self.host_port}/hub/service/register",
                json={"name": self.name, "prefix": f"/svc/{self.name}",
                      "pid": None, "start": [], "cwd": ""},
                headers={"Authorization": f"Bearer {self.token}"},
            )
        assert r.status_code == 200, r.text
        body = r.json()
        self._service_id = body["service_id"]
        return self._service_id

    async def open_control(self) -> None:
        await self.register()
        url = (
            f"ws://127.0.0.1:{self.host_port}"
            f"/hub/service/control/{self._service_id}"
        )
        self._control_ws = await websockets.connect(
            url,
            subprotocols=[f"bearer.{self.token}"],
            max_size=None,
            open_timeout=5,
        )
        # Send the ready frame so the channel flips to ready.
        await self._control_ws.send(json.dumps({"kind": "ready", "api": self.api}))
        self._reader_task = asyncio.create_task(self._reader())
        self._writer_task = asyncio.create_task(self._writer())
        # Wait for the hub-side ControlChannel.ready to flip — the
        # /svc/X handlers gate on it.
        for _ in range(50):
            from awm.services.hub import rpc as rpc_mod
            ch = rpc_mod.get_control(self._service_id)
            if ch is not None and ch.ready.is_set():
                return
            await asyncio.sleep(0.02)
        raise RuntimeError("hub never marked control channel ready")

    async def _reader(self) -> None:
        try:
            async for frame in self._control_ws:
                env = json.loads(frame)
                self.received.append(env)
        except Exception:
            return

    async def _writer(self) -> None:
        try:
            while True:
                env = await self._send_q.get()
                await self._control_ws.send(json.dumps(env))
        except Exception:
            return

    def send(self, env: dict[str, Any]) -> None:
        self._send_q.put_nowait(env)

    async def wait_for(self, predicate, timeout: float = 3.0) -> dict[str, Any]:
        """Poll self.received until predicate(env) matches; return that env."""
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            for env in self.received:
                if predicate(env):
                    return env
            await asyncio.sleep(0.02)
        raise asyncio.TimeoutError(
            f"no envelope matched in {timeout}s; received={self.received!r}",
        )

    async def open_bridge(self, bridge_id: str):
        url = (
            f"ws://127.0.0.1:{self.host_port}"
            f"/hub/service/bridge/{self._service_id}/{bridge_id}"
        )
        ws = await websockets.connect(
            url,
            subprotocols=[f"bearer.{self.token}"],
            max_size=None,
            open_timeout=5,
        )
        return ws

    async def close(self) -> None:
        for t in (self._reader_task, self._writer_task):
            if t is not None:
                t.cancel()
        if self._control_ws is not None:
            try:
                await self._control_ws.close()
            except Exception:
                pass


@asynccontextmanager
async def fake_service(host_port: int, token: str, *, name: str,
                       api: dict[str, Any]):
    svc = FakeService(host_port, token, name, api)
    await svc.open_control()
    try:
        yield svc
    finally:
        await svc.close()


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


class TestServiceRegister:
    def test_post_returns_control_and_bridge_paths(self, running_hub):
        port, token = running_hub["port"], running_hub["token"]
        r = httpx.post(
            f"http://127.0.0.1:{port}/hub/service/register",
            json={"name": "svc-a", "prefix": "/svc/svc-a",
                  "pid": 4242, "start": ["start.sh"], "cwd": "/tmp"},
            headers={"Authorization": f"Bearer {token}"},
            timeout=5,
        )
        assert r.status_code == 200, r.text
        body = r.json()
        sid = body["service_id"]
        assert body["control_ws_path"] == f"/hub/service/control/{sid}"
        assert body["bridge_ws_base"] == f"/hub/service/bridge/{sid}"
        assert body["prefix"] == "/svc/svc-a"

    def test_prefix_must_begin_svc(self, running_hub):
        port, token = running_hub["port"], running_hub["token"]
        r = httpx.post(
            f"http://127.0.0.1:{port}/hub/service/register",
            json={"name": "bad", "prefix": "/notsvc",
                  "pid": None, "start": [], "cwd": ""},
            headers={"Authorization": f"Bearer {token}"},
            timeout=5,
        )
        assert r.status_code == 409


# ---------------------------------------------------------------------------
# RPC call / notify dispatch via /svc/X/fn/<name>
# ---------------------------------------------------------------------------


class TestFnDispatch:
    def test_call_round_trip(self, running_hub):
        port, token = running_hub["port"], running_hub["token"]

        async def run():
            async with fake_service(
                port, token, name="echo",
                api={"functions": [{"name": "echo"}]},
            ) as svc:
                # Auto-reply to any `call` envelope with ok=True, result echoed.
                async def auto_replier():
                    seen: set[str] = set()
                    while True:
                        for env in list(svc.received):
                            cid = env.get("id")
                            if env.get("kind") != "call" or cid in seen:
                                continue
                            seen.add(cid)
                            svc.send({"kind": "reply", "id": cid,
                                       "ok": True,
                                       "result": {"got": env.get("args")}})
                        await asyncio.sleep(0.01)

                replier_task = asyncio.create_task(auto_replier())
                try:
                    async with httpx.AsyncClient(timeout=5) as client:
                        r = await client.post(
                            f"http://127.0.0.1:{port}/svc/echo/fn/echo",
                            json={"x": 1, "y": "two"},
                        )
                    assert r.status_code == 200, r.text
                    assert r.json() == {"got": {"x": 1, "y": "two"}}
                finally:
                    replier_task.cancel()

        asyncio.new_event_loop().run_until_complete(run())

    def test_notify_returns_202_and_no_reply_needed(self, running_hub):
        port, token = running_hub["port"], running_hub["token"]

        async def run():
            async with fake_service(
                port, token, name="noti",
                api={"functions": [{"name": "ping", "no_response": True}]},
            ) as svc:
                async with httpx.AsyncClient(timeout=5) as client:
                    r = await client.post(
                        f"http://127.0.0.1:{port}/svc/noti/fn/ping",
                        json={"hello": True},
                    )
                assert r.status_code == 202
                # Confirm the service saw a notify envelope (no id).
                env = await svc.wait_for(lambda e: e.get("kind") == "notify")
                assert env["fn"] == "ping"
                assert env["args"] == {"hello": True}
                assert "id" not in env

        asyncio.new_event_loop().run_until_complete(run())

    def test_call_504_on_service_silence(self, running_hub):
        """When the service never replies, the HTTP caller gets a clean 504,
        not a hang or 500."""
        port, token = running_hub["port"], running_hub["token"]

        async def run():
            async with fake_service(
                port, token, name="silent",
                api={"functions": [{"name": "f", "timeout": 0.3}]},
            ):
                async with httpx.AsyncClient(timeout=5) as client:
                    r = await client.post(
                        f"http://127.0.0.1:{port}/svc/silent/fn/f",
                        json={},
                    )
                assert r.status_code == 504, r.text

        asyncio.new_event_loop().run_until_complete(run())

    def test_call_502_on_service_error_reply(self, running_hub):
        port, token = running_hub["port"], running_hub["token"]

        async def run():
            async with fake_service(
                port, token, name="errsvc",
                api={"functions": [{"name": "bad"}]},
            ) as svc:
                async def replier():
                    while True:
                        for env in list(svc.received):
                            if env.get("kind") == "call":
                                svc.send({"kind": "reply", "id": env["id"],
                                           "ok": False, "error": "intentional"})
                                return
                        await asyncio.sleep(0.01)

                task = asyncio.create_task(replier())
                try:
                    async with httpx.AsyncClient(timeout=5) as client:
                        r = await client.post(
                            f"http://127.0.0.1:{port}/svc/errsvc/fn/bad",
                            json={},
                        )
                    assert r.status_code == 502
                    assert "intentional" in r.text
                finally:
                    task.cancel()

        asyncio.new_event_loop().run_until_complete(run())


# ---------------------------------------------------------------------------
# Emitter sub/emit fan-out via /svc/X/emit/<topic>
# ---------------------------------------------------------------------------


class TestEmitterFanout:
    def test_subscriber_receives_emitted_payload(self, running_hub):
        port, token = running_hub["port"], running_hub["token"]

        async def run():
            async with fake_service(
                port, token, name="tick",
                api={"emitters": [{"topic": "beat"}]},
            ) as svc:
                ws_url = f"ws://127.0.0.1:{port}/svc/tick/emit/beat"
                async with websockets.connect(
                    ws_url,
                    subprotocols=[f"bearer.{token}"],
                    max_size=None,
                    open_timeout=5,
                ) as browser_ws:
                    # Wait for the hub to send the `sub` envelope upstream.
                    await svc.wait_for(lambda e: e.get("kind") == "sub")
                    # Service emits a payload — browser receives JSON text.
                    svc.send({"kind": "emit", "topic": "beat",
                               "payload": {"n": 5, "msg": "go"}})
                    raw = await asyncio.wait_for(browser_ws.recv(), timeout=3)
                    assert json.loads(raw) == {"n": 5, "msg": "go"}

        asyncio.new_event_loop().run_until_complete(run())


# ---------------------------------------------------------------------------
# Direct-bridge session round-trip via /svc/X/session/<id>
# ---------------------------------------------------------------------------


class TestDirectSessionBridge:
    def test_byte_relay_both_directions(self, running_hub):
        port, token = running_hub["port"], running_hub["token"]

        async def run():
            async with fake_service(
                port, token, name="stream",
                api={"sessions": [{"kind": "pcm", "transport": "direct"}]},
            ) as svc:
                # Background: when the service sees session.open, ack it
                # and open the bridge WS.
                bridge_ws_box: dict[str, Any] = {}

                async def open_handler():
                    env = await svc.wait_for(
                        lambda e: e.get("kind") == "session.open",
                    )
                    bid = env["bridge_id"]
                    sid = env["session_id"]
                    bws = await svc.open_bridge(bid)
                    bridge_ws_box["ws"] = bws
                    # Acknowledge so the browser-side open_session_via_http
                    # call returns to the test.
                    svc.send({"kind": "session.opened", "session_id": sid,
                               "ok": True})

                handler_task = asyncio.create_task(open_handler())
                try:
                    # Browser-side: POST opens session, returns ws_path.
                    async with httpx.AsyncClient(timeout=5) as client:
                        r = await client.post(
                            f"http://127.0.0.1:{port}/svc/stream/session/pcm",
                            json={"init": "go"},
                        )
                    assert r.status_code == 200, r.text
                    body = r.json()
                    assert body["direct"] is True
                    ws_path = body["ws_path"]

                    await asyncio.wait_for(handler_task, timeout=3)
                    upstream_ws = bridge_ws_box["ws"]
                    assert upstream_ws is not None

                    # Browser opens the session WS.
                    browser_url = f"ws://127.0.0.1:{port}{ws_path}"
                    async with websockets.connect(
                        browser_url,
                        subprotocols=[f"bearer.{token}"],
                        max_size=None,
                        open_timeout=5,
                    ) as browser_ws:
                        # Browser → service: binary bytes.
                        await browser_ws.send(b"abc123")
                        got_up = await asyncio.wait_for(
                            upstream_ws.recv(), timeout=3,
                        )
                        assert got_up == b"abc123"

                        # Service → browser: binary bytes.
                        await upstream_ws.send(b"xyz789")
                        got_down = await asyncio.wait_for(
                            browser_ws.recv(), timeout=3,
                        )
                        assert got_down == b"xyz789"

                    # Cleanup: bridge ws may already be closed by the relay.
                    try:
                        await upstream_ws.close()
                    except Exception:
                        pass
                finally:
                    handler_task.cancel()

        asyncio.new_event_loop().run_until_complete(run())
