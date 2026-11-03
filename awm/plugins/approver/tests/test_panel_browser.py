"""The panel in Chromium with a CDP virtual authenticator, framed by a stand-in board page."""

from __future__ import annotations

import asyncio
import os
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import uvicorn

from awm.approver.core import Approver, Config
from awm.approver.server import create_app
from awm.taskqueue.store import Store

from conftest import SECRET, FakeTools

playwright = pytest.importorskip("playwright.sync_api")

BOARD = """<!doctype html><meta charset=utf-8><title>board</title>
<iframe id=p src="{panel}/panel" allow="publickey-credentials-get; publickey-credentials-create"
 style="width:900px;height:700px"></iframe>
<script>
window.ready = false;
addEventListener("message", (e) => {{ if (e.origin === "{panel}" && e.data.type === "awm-panel-ready") window.ready = true; }});
window.pick = (id, sha, preview) => document.getElementById("p").contentWindow.postMessage(
  {{type: "awm-card", task_id: id, body_sha256: sha, preview}}, "{panel}");
</script>"""


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Server:
    """uvicorn in a thread with its own loop, so the test can call the approver on that loop."""

    def __init__(self, app, port: int) -> None:
        self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port,
                                                    log_level="warning"))
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_until_complete(self.server.serve())

    def start(self) -> "Server":
        self.thread.start()
        while not self.server.started:
            time.sleep(0.05)
        return self

    def call(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(10)

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(5)


@pytest.fixture
def stack(tmp_path, keys):
    port, board_port = free_port(), free_port()
    origin, board = f"http://localhost:{port}", f"http://127.0.0.1:{board_port}"
    store = Store(tmp_path / "queue.db", action_secret=SECRET)
    tools = FakeTools(store)
    ap = Approver(tmp_path / "approver.db", keys,
                  Config(origin=origin, allowed_orders={"spawn": ["*"], "zotero_sync": ["deneb"]}),
                  tools, SECRET)
    srv = Server(create_app(ap, board_origins=[board], peers=["deneb", "altair"]), port).start()
    page_html = BOARD.format(panel=origin).encode()

    class Board(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(page_html)

        def log_message(self, *a):
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", board_port), Board)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield ap, store, tools, srv, origin, board
    httpd.shutdown()
    srv.stop()


def chromium_env() -> dict[str, str]:
    """Chromium's shared libraries come from the env (conda-forge nss, atk, libgbm, ...), not the host."""
    lib = os.path.join(sys.prefix, "lib")
    return {**os.environ, "LD_LIBRARY_PATH": ":".join(filter(None, [lib, os.environ.get("LD_LIBRARY_PATH")]))}


def wait_for(fn, timeout=10.0):
    end = time.time() + timeout
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(0.1)
    raise AssertionError("timed out")


def test_panel_end_to_end(stack):
    ap, store, tools, srv, origin, board = stack
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--no-sandbox"], env=chromium_env())
        ctx = browser.new_context()
        page = ctx.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        cdp = ctx.new_cdp_session(page)
        cdp.send("WebAuthn.enable")
        cdp.send("WebAuthn.addVirtualAuthenticator", {"options": {
            "protocol": "ctap2", "transport": "internal", "hasResidentKey": True,
            "hasUserVerification": True, "isUserVerified": True,
            "automaticPresenceSimulation": True}})

        # Enrol through a one-time link (R85).
        link = ap.enrol_link()
        page.goto(link["url"].replace("https://approve.test", origin))
        page.fill("#name", "test-browser")
        page.click("#enrol")
        page.wait_for_selector("#status[data-kind=ok]")
        assert "Enrolled" in page.inner_text("#status"), errors
        assert ap.devices()[0]["name"] == "test-browser"

        # The board frames the panel and selects a Proposed card.
        card = store.task_open("Rework the tunnel", section="proposed", kind="note")
        page.goto(board)
        wait_for(lambda: page.evaluate("window.ready"))
        frame = page.frame_locator("#p")
        page.evaluate("([id]) => window.pick(id, '" + "ab" * 32 + "', 'First lines')",
                      [card["task_id"]])
        frame.locator("h2").filter(has_text="Rework the tunnel").wait_for()
        frame.locator("select.peer").first.select_option("deneb")
        frame.locator("button", has_text="Spawn agent").click()
        wait_for(lambda: store.task_get(card["task_id"])["section"] == "working")
        envs = srv.call(ap.order_pull("deneb"))
        assert envs and envs[0]["order"]["payload"]["body_sha256"] == "ab" * 32

        # The 2fa strip is always there. Open a window for bursts, then press with no prompt.
        frame.locator("button", has_text="Arm 2").wait_for()
        frame.locator("button", has_text="Arm 2").click()
        wait_for(lambda: ("burst", {"device": "phone", "count": 2}) in tools.twofa_calls)
        frame.locator("li", has_text="u1").locator("button", has_text="Approve").click()
        wait_for(lambda: any(c[0] == "approve" for c in tools.twofa_calls))

        # An approval request card: Approve signs a grant.
        req = srv.call(ap.approval_request("browser_signin", reason="github for s1",
                                           payload={"seat": "s1", "site": "github"}))
        page.evaluate("([id]) => window.pick(id, null, '')", [req["task_id"]])
        frame.locator("#card h2", has_text="browser_signin").wait_for()
        frame.locator("#card button.yes", has_text="Approve").click()
        wait_for(lambda: ap.approval_get(req["request_id"])["status"] == "approved")

        # A drop mid-action: the message is lost, the next press resumes and alcor takes it.
        seq = ap.devices()[0]["seq"]
        page.route(f"{origin}/api/act", lambda route: route.abort())
        frame.locator("input[placeholder='New note title']").fill("Lost then found")
        frame.locator("button", has_text="New note").click()
        frame.locator("#status[data-kind=error]").wait_for()
        page.unroute(f"{origin}/api/act")
        assert ap.devices()[0]["seq"] == seq
        frame.locator("button", has_text="New note").click()
        wait_for(lambda: len(store.task_list(section="proposed", q="Lost then found")["cards"]) >= 1)
        assert len(store.task_list(section="proposed", q="Lost then found")["cards"]) == 1
        assert not ap.devices()[0]["locked"]

        # A window: one passkey, then in-scope presses go signed by the device key alone.
        frame.locator("details.window summary").click()
        frame.locator("details.window input[value=twofa_burst]").check()
        frame.locator("details.window select").select_option("any")
        frame.locator("button", has_text="Approve with passkey").click()
        wait_for(lambda: any(w["closed_at"] is None for w in ap.windows(ap.devices()[0]["device_id"])))
        frame.locator("button", has_text="Arm 4").click()
        wait_for(lambda: ("burst", {"device": "phone", "count": 4}) in tools.twofa_calls)
        last = ap._one("SELECT action FROM action_log ORDER BY id DESC LIMIT 1")["action"]
        assert last == "twofa_burst"

        # The embeddable button (redux-fleet's Zotero Sync now) issues a signed order.
        page.goto(f"{origin}/panel/button?kind=zotero_sync&node=deneb&label=Sync%20now")
        page.click("#go")
        envs = wait_for(lambda: srv.call(ap.order_pull("deneb", ["zotero_sync"])))
        ap.order_done(envs[0]["order"]["order_id"], True, "synced 3 items")
        page.wait_for_selector("#status:has-text('synced 3 items')", timeout=10000)
        assert not errors, errors
        browser.close()
