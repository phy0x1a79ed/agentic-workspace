"""What the hub puts on the wire for a request that has no body.

The URL proxy used to hand httpx ``content=request.stream()`` for every
method. With no ``Content-Length`` httpx frames that as chunked, so a plain
``GET`` went upstream with ``Transfer-Encoding: chunked`` and a zero-length
chunk terminator glued to the end of the headers.

An upstream that reads request bodies never notices — it drains the empty body
and answers normally. That is why every other url-proxy test in this suite
passed straight through the defect: they all use a uvicorn upstream. An
upstream that does *not* read bodies parses that terminator as the next request
line on a keep-alive connection, answers ``400`` and closes — and by then the
hub's pooled client has handed that socket to a different request, which fails
with ``RemoteProtocolError`` and used to escape as a gateway ``500``.

So the upstream here is a stdlib ``ThreadingHTTPServer`` that deliberately
never touches ``rfile`` on ``GET``: the shape of the drawio view listener, the
one live ``kind=url`` mount this actually broke.

A sequential "issue N GETs and assert 200" loop does **not** catch this —
httpcore sees the poisoned socket as readable at next checkout and dials a
fresh one, so it self-heals and the failure only shows up under concurrency.
The assertions below are on the forwarded headers instead, which is
deterministic.
"""

from __future__ import annotations


import pytest
pytestmark = [pytest.mark.hub, pytest.mark.slow]

import contextlib
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from fastapi.testclient import TestClient


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class Recorder:
    """What the upstream saw, across every connection it accepted."""

    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.errors: list[tuple[int, str]] = []
        self.lock = threading.Lock()

    def framing(self, index: int = 0) -> dict:
        h = self.requests[index]["headers"]
        return {
            "content-length": h.get("content-length"),
            "transfer-encoding": h.get("transfer-encoding"),
        }


def _make_handler(rec: Recorder):
    class Handler(BaseHTTPRequestHandler):
        # Keep-alive is the whole point: without it the poisoned socket is
        # closed before anything can be parsed off it.
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):  # silence
            pass

        def _record(self, body: bytes | None) -> None:
            with rec.lock:
                rec.requests.append({
                    "method": self.command,
                    "path": self.path,
                    "headers": {k.lower(): v for k, v in self.headers.items()},
                    "body": body,
                })

        def send_error(self, code, message=None, explain=None):
            with rec.lock:
                rec.errors.append((code, str(message)))
            super().send_error(code, message, explain)

        def _respond(self, payload: bytes, *, body: bool = True) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            if body:
                self.wfile.write(payload)

        def do_GET(self):
            # Deliberately never reads rfile — the behaviour under test.
            self._record(None)
            self._respond(b"ok")

        def do_HEAD(self):
            self._record(None)
            self._respond(b"ok", body=False)

        def do_POST(self):
            n = int(self.headers.get("content-length") or 0)
            self._record(self.rfile.read(n) if n else b"")
            self._respond(b"ok")

    return Handler


@contextlib.contextmanager
def _bodyless_upstream():
    rec = Recorder()
    port = _free_port()
    httpd = ThreadingHTTPServer(("127.0.0.1", port), _make_handler(rec))
    th = threading.Thread(target=httpd.serve_forever, daemon=True)
    th.start()
    try:
        yield port, rec
    finally:
        httpd.shutdown()
        httpd.server_close()
        th.join(timeout=2)


def _quiesce(rec: Recorder, window: float = 0.4) -> None:
    """Let the upstream finish reacting to whatever is still on the socket.

    The bogus second "request" is parsed after the hub already has its
    response, so an immediate assertion on ``rec.errors`` would race it.
    """
    deadline = time.monotonic() + window
    seen = (len(rec.requests), len(rec.errors))
    while time.monotonic() < deadline:
        time.sleep(0.05)
        now = (len(rec.requests), len(rec.errors))
        if now != seen:
            seen = now
            deadline = time.monotonic() + window


@pytest.fixture(autouse=True)
def _clean_registry():
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


@contextlib.contextmanager
def _mounted(hub_client):
    with _bodyless_upstream() as (port, rec):
        r = hub_client.post(
            "/hub/register",
            json={"name": "bodyless", "prefix": "/bodyless",
                  "url": f"http://127.0.0.1:{port}"},
        )
        assert r.status_code == 200, r.text
        yield rec


class TestBodylessFraming:
    def test_get_is_forwarded_with_no_body_framing(self, hub_client):
        with _mounted(hub_client) as rec:
            r = hub_client.get("/bodyless/thing")
            assert r.status_code == 200, r.text
            assert rec.framing() == {
                "content-length": None, "transfer-encoding": None,
            }

    def test_head_is_forwarded_with_no_body_framing(self, hub_client):
        with _mounted(hub_client) as rec:
            r = hub_client.head("/bodyless/thing")
            assert r.status_code == 200, r.text
            assert rec.framing() == {
                "content-length": None, "transfer-encoding": None,
            }

    def test_a_bodyless_get_leaves_nothing_on_the_socket(self, hub_client):
        """The symptom, not the cause: a stray chunk terminator becomes a
        second, malformed request line on the same keep-alive connection."""
        with _mounted(hub_client) as rec:
            assert hub_client.get("/bodyless/thing").status_code == 200
            _quiesce(rec)
            assert rec.errors == []
            assert len(rec.requests) == 1

    def test_repeated_bodyless_gets_all_succeed(self, hub_client):
        with _mounted(hub_client) as rec:
            for _ in range(5):
                assert hub_client.get("/bodyless/thing").status_code == 200
            _quiesce(rec)
            assert rec.errors == []
            assert len(rec.requests) == 5


class TestSizedBodyStillForwarded:
    """The blast radius: anything that *did* frame a body must be untouched."""

    def test_post_with_content_length(self, hub_client):
        with _mounted(hub_client) as rec:
            r = hub_client.post("/bodyless/thing", content=b"hello world")
            assert r.status_code == 200, r.text
            assert rec.requests[0]["body"] == b"hello world"
            assert rec.framing() == {
                "content-length": "11", "transfer-encoding": None,
            }

    def test_zero_length_post_keeps_its_content_length(self, hub_client):
        with _mounted(hub_client) as rec:
            r = hub_client.post("/bodyless/thing")
            assert r.status_code == 200, r.text
            assert rec.requests[0]["body"] == b""
            assert rec.framing() == {
                "content-length": "0", "transfer-encoding": None,
            }
            _quiesce(rec)
            assert rec.errors == []
