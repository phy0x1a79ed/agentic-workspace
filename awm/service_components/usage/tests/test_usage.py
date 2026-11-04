"""awm.usage sends each event the way the contract says, and never raises."""

from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import awm.usage as usage
from awm.gatewayclient.adapter import ServiceAdapter


@pytest.fixture
def gateway(monkeypatch):
    got: list[dict] = []
    seen = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            got.append({"path": self.path, **json.loads(self.rfile.read(int(self.headers["Content-Length"])))})
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"ok":true}')
            seen.set()

        def log_message(self, *a):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv("AWM_HUB_URL", f"http://127.0.0.1:{server.server_address[1]}")
    monkeypatch.delenv("AWM_PLUGIN", raising=False)
    monkeypatch.delenv("AWM_SERVICE_NAME", raising=False)
    monkeypatch.setattr(ServiceAdapter, "current", None)
    yield got, seen
    server.shutdown()


def _wait(seen):
    assert seen.wait(5)


def test_code_use_posts_a_code_event(gateway):
    got, seen = gateway
    usage.record_code("kb", "vectors")
    _wait(seen)
    assert got == [{"path": "/usage", "plugin": "kb", "surface": "code", "name": "vectors"}]


def test_a_run_outside_a_service_posts_under_awm_plugin(gateway, monkeypatch):
    got, seen = gateway
    monkeypatch.setenv("AWM_PLUGIN", "rlm-factorio")
    usage.record_background("lab", ok=False, ms=12)
    _wait(seen)
    assert got[0] == {"path": "/usage", "plugin": "rlm-factorio", "surface": "background",
                      "name": "lab", "ok": False, "ms": 12}


def test_a_run_with_no_plugin_name_is_dropped_quietly(gateway):
    got, _ = gateway
    usage.record_background("lab")
    assert got == []


def test_a_gateway_that_is_down_never_raises(monkeypatch):
    monkeypatch.setenv("AWM_HUB_URL", "http://127.0.0.1:9")
    usage.record_code("kb", "vectors")
    usage._drain()


def test_background_run_times_the_block_and_counts_a_raise_as_failed(monkeypatch):
    calls = []
    monkeypatch.setattr(usage, "record_background", lambda job, ok, ms, plugin=None: calls.append((job, ok, ms)))
    with usage.background_run("sweep"):
        pass
    with pytest.raises(KeyError):
        with usage.background_run("sweep"):
            raise KeyError("x")
    assert [(j, ok) for j, ok, _ in calls] == [("sweep", True), ("sweep", False)]
    assert all(isinstance(ms, int) and ms >= 0 for *_, ms in calls)


class _FakeWS:
    def __init__(self):
        self.sent = []

    async def send(self, text):
        self.sent.append(json.loads(text))


def _adapter(monkeypatch, loop):
    adapter = ServiceAdapter("transcripts", {"functions": []}, {})
    adapter._control_ws = _FakeWS()
    adapter.loop = loop
    monkeypatch.setattr(ServiceAdapter, "current", adapter)
    return adapter


async def test_inside_a_service_a_run_goes_as_a_control_frame(monkeypatch):
    adapter = _adapter(monkeypatch, asyncio.get_running_loop())
    usage.record_background("nightly", ok=True, ms=840)
    await asyncio.to_thread(usage.record_background, "sweep")
    for _ in range(50):
        if len(adapter._control_ws.sent) == 2:
            break
        await asyncio.sleep(0.01)
    assert adapter._control_ws.sent == [
        {"kind": "usage", "surface": "background", "name": "nightly", "ok": True, "ms": 840},
        {"kind": "usage", "surface": "background", "name": "sweep", "ok": True, "ms": None},
    ]


async def test_between_reconnects_a_run_posts_under_the_service_name(gateway, monkeypatch):
    got, seen = gateway
    adapter = _adapter(monkeypatch, asyncio.get_running_loop())
    adapter._control_ws = None
    usage.record_background("sweep")
    await asyncio.to_thread(_wait, seen)
    assert got[0]["plugin"] == "transcripts" and got[0]["surface"] == "background"


async def test_a_caller_naming_another_plugin_posts_even_inside_a_service(gateway, monkeypatch):
    got, seen = gateway
    adapter = _adapter(monkeypatch, asyncio.get_running_loop())
    with usage.background_run("fxlab", plugin="rlm-factorio"):
        pass
    await asyncio.to_thread(_wait, seen)
    assert adapter._control_ws.sent == []
    assert (got[0]["plugin"], got[0]["name"], got[0]["ok"]) == ("rlm-factorio", "fxlab", True)
