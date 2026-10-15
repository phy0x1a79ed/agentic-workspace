"""The gateway mount contract: what gets registered, and how the listener
reads what the gateway forwards.

No live gateway anywhere here -- a stand-in HTTP server plays the hub's
``/hub/register`` endpoint for the one test that needs to see an actual
registration payload, exactly the way :mod:`test_view` fakes the exporter
instead of touching a real Penpot. The failures worth pinning:

* A mount registered ``kind=static`` (or missing its ``url`` target
  altogether) would 404 every render forever -- a silent, total outage that
  looks like a normal gateway 404 rather than a broken deploy.
* A mount name that collided with a future ``penpot`` supervision service's
  own record would fail to register at all (last-writer-wins or an outright
  409, depending on which registered first), and do so nondeterministically
  across restarts.
* A ``Cache`` built without ``freshness`` wired in degrades to plain TTL
  expiry -- correct-looking, silently worse -- so the one thing worth
  confirming about :func:`~awm.penpot_view.mount.build_view_server` is that it
  never takes that path.
* If the gateway ever started stripping the ``/penpot-view`` prefix before
  forwarding (the way ``strip_prefix: true`` mounts work), the listener --
  which parses relative to the *full* prefix -- would 404 every request
  instead of silently misrouting; the point of ``kind=url`` without
  ``strip_prefix`` is that the full path always arrives intact.
"""

from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from awm.penpot_view import mount as M
from awm.penpot_view import renderspec as R
from awm.penpot_view import view as V
from awm.penpot_view.exporter_client import ExporterClient

FILE_ID = "0197f9d2-1a2b-73aa-8b9c-1234567890ab"
PAGE_ID = "0197f9d2-1a2b-73aa-8b9c-1234567890ac"
BOARD_ID = "0197f9d2-1a2b-73aa-8b9c-1234567890ad"


# --- a fake gateway, for the one test that inspects the register payload ---

class _FakeHub:
    """Answers one ``POST /hub/register``, capturing the body, then hands
    back a lease path nothing will ever connect to -- the retry/reconnect
    loop is :mod:`awm.penpot_view.view`'s own concern, not this module's."""

    def __init__(self) -> None:
        self.bodies: list[dict] = []
        self.got_one = threading.Event()
        self.httpd = HTTPServer(("127.0.0.1", 0), self._make_handler())
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(
            target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def _make_handler(self):
        outer = self

        class _Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 -- stdlib naming
                length = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(length) or b"{}")
                outer.bodies.append(body)
                payload = json.dumps({
                    "service_id": "test-sid",
                    "lease_ws_path": "/hub/lease/test-sid",
                }).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                outer.got_one.set()

            def log_message(self, *args) -> None:  # silence stdlib access log
                return

        return _Handler

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)


@pytest.fixture()
def fake_hub():
    hub = _FakeHub()
    yield hub
    hub.stop()


# --- what gets registered ------------------------------------------------

def test_view_prefix_is_the_renderspec_prefix():
    assert M.VIEW_PREFIX == R.VIEW_PREFIX == "/penpot-view"


def test_mount_name_cannot_collide_with_a_penpot_service_record():
    """A future ``penpot`` supervision service will own the plain ``penpot``
    name; this mount answers to something else entirely."""
    assert M.MOUNT_NAME == "penpot-view"
    assert M.MOUNT_NAME != "penpot"


def test_register_payload_is_kind_url_not_static(fake_hub, monkeypatch):
    monkeypatch.setenv("AWM_HUB_URL", fake_hub.url)
    view = M.build_view_server()

    async def _run() -> None:
        task = asyncio.create_task(view.hold_mount())
        try:
            await asyncio.wait_for(
                asyncio.get_event_loop().run_in_executor(
                    None, fake_hub.got_one.wait, 10),
                timeout=11,
            )
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    asyncio.run(_run())

    assert fake_hub.bodies, "hold_mount never registered"
    body = fake_hub.bodies[0]
    assert body["name"] == M.MOUNT_NAME
    assert body["prefix"] == M.VIEW_PREFIX
    assert "url" in body, "a url target is what makes this a kind=url mount"
    assert "static" not in body, \
        "a kind=static mount cannot render on demand; every request would 404"


# --- how the pipeline is wired -------------------------------------------

def test_cache_freshness_is_wired_to_the_exporters_file_etag():
    """Without this, ``Cache`` degrades to plain TTL expiry -- correct-looking
    and materially weaker (see ``view.py``'s module docstring). A build that
    forgot to pass ``freshness`` would still run; this is what would catch it."""
    view = M.build_view_server()
    cache = view.renderer.cache
    assert cache.freshness is not None
    assert cache.freshness.__func__ is ExporterClient.file_etag


def test_build_view_server_is_a_fresh_instance_each_call():
    """The factory must have no shared state -- a test (or a future caller)
    reusing it must never observe another caller's listener or lease."""
    a = M.build_view_server()
    b = M.build_view_server()
    assert a is not b
    assert a.renderer is not b.renderer


def test_view_server_singleton_is_built_once(monkeypatch):
    monkeypatch.setattr(M, "_VIEW", None)
    a = M.view_server()
    b = M.view_server()
    assert a is b


# --- the handler reads what the gateway actually forwards -----------------

def test_handler_parses_the_full_unstripped_path():
    path = f"{V.VIEW_PREFIX}/{FILE_ID}/{PAGE_ID}/{BOARD_ID}"
    assert V.parse_path(path) == (FILE_ID, PAGE_ID, BOARD_ID)


def test_a_stripped_prefix_would_404_not_silently_misroute():
    """The gateway forwards the full path for a ``kind=url`` mount without
    ``strip_prefix`` -- if that ever changed, the handler (which parses
    relative to the whole ``/penpot-view/...`` prefix) must fail loudly
    rather than resolve the wrong board."""
    stripped = f"/{FILE_ID}/{PAGE_ID}/{BOARD_ID}"
    with pytest.raises(V.ViewError) as excinfo:
        V.parse_path(stripped)
    assert excinfo.value.status == 404
