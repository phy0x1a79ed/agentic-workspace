"""The Swarm board page in a real Trilium, framing the approver panel (queue.md R61 to R63).

Needs a running Trilium: ``AWM_TEST_TRILIUM="URL TOKEN"`` from tests/trilium-up.sh.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from awm.operations.approver.core import Approver, Config
from awm.operations.approver.server import create_app
from awm.operations import board
from awm.operations.board import card_note_id
from awm.operations.relay import Refused
from awm.operations.trilium import TriliumTool
from awm.operations.store import Store
from awm.operations.writer import Writer

from conftest import FakeTools
from test_panel_browser import Server, chromium_env, free_port, wait_for

playwright = pytest.importorskip("playwright.sync_api")
PASSWORD = "awm-queue-test"  # trilium-up.sh sets it


@pytest.fixture
def trilium():
    env = os.environ.get("AWM_TEST_TRILIUM")
    if not env:
        pytest.skip("set AWM_TEST_TRILIUM to 'URL TOKEN' from tests/trilium-up.sh")
    url, token = env.split()
    return url, token


class VaultTool:
    """The real ``trilium`` tool's handlers against the test vault, called as alcor's writer.

    The handlers send no credential, because the vault's own login is off on deneb.
    The test container keeps its login, so its token rides on every request.
    """

    def __init__(self, url: str, token: str, monkeypatch: pytest.MonkeyPatch) -> None:
        sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "services" / "trilium"))
        from awm import config
        from awm.trilium import etapi, hub_adapter

        self.handlers, self.errors = hub_adapter.HANDLERS, (etapi.EtapiError, PermissionError)
        request = etapi.httpx.request
        monkeypatch.setattr(config, "VAULT_URL", url.rstrip("/"))
        monkeypatch.setattr(etapi.httpx, "request", lambda method, u, headers=None, **kw: request(
            method, u, headers={**(headers or {}), "Authorization": token}, **kw))

    def call_sync(self, verb: str, args: dict[str, Any], **_: Any) -> Any:
        coro = self.handlers[verb](dict(args), "peer:alcor")
        try:  # on its own thread: Playwright's sync API holds this one's loop
            with ThreadPoolExecutor(1) as pool:
                return pool.submit(asyncio.run, coro).result()
        except self.errors as exc:
            raise Refused(str(exc)) from None


@pytest.fixture
def stack(tmp_path, keys, trilium, monkeypatch):
    url, token = trilium
    port = free_port()
    origin = f"http://localhost:{port}"
    store = Store(tmp_path / "queue.db")
    api = TriliumTool(tool=VaultTool(url, token, monkeypatch))
    writer = Writer(store, api)
    store.body_reader = writer.read_body
    ap = Approver(tmp_path / "approver.db", keys, Config(origin=origin, allowed_orders={"spawn": ["*"]}),
                  store, FakeTools())
    srv = Server(create_app(ap, board_origins=[url], peers=["deneb", "altair"]), port).start()
    board.install(api, origin)  # also refreshes the page notes from board/
    yield ap, store, writer, srv, origin, url
    srv.stop()


def test_board_in_trilium(stack):
    ap, store, writer, srv, origin, url = stack
    run = os.urandom(3).hex()
    proposed = store.task_open(f"Tunnel rework {run}", section="proposed", kind="note")["task_id"]
    working = store.task_open(f"Vault backup {run}", "cc-w1", peer="altair", name="vault-backup",
                              rc_url="https://claude.ai/code/session_w1")["task_id"]
    done = store.task_open(f"Old chore {run}", "cc-d1")["task_id"]
    store.task_report(done, "cc-d1", "done")
    writer.step()
    body = f"# Tunnel rework {run}\n\nMove the tunnel to the new edge.\n"
    writer.api.update(card_note_id(proposed), content=body)

    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--no-sandbox"], env=chromium_env())
        ctx = browser.new_context(viewport={"width": 1600, "height": 1000})
        page = ctx.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        cdp = ctx.new_cdp_session(page)
        cdp.send("WebAuthn.enable")
        cdp.send("WebAuthn.addVirtualAuthenticator", {"options": {
            "protocol": "ctap2", "transport": "internal", "hasResidentKey": True,
            "hasUserVerification": True, "isUserVerified": True,
            "automaticPresenceSimulation": True}})

        page.goto(ap.enrol_link()["url"].replace("https://approve.test", origin))
        page.fill("#name", "board-browser")
        page.click("#enrol")
        page.wait_for_selector("#status[data-kind=ok]")

        assert ctx.request.post(f"{url}/login", form={"password": PASSWORD}).ok
        page.goto(f"{url}/?extraWindow=1#root/{board.ROOT_ID}")  # one tab, none restored
        board_el = page.locator(".awm-board:visible")  # Trilium may restore older tabs too
        sections = board_el.locator(".awm-sections")
        sections.locator("details[data-section=done]").wait_for(state="attached", timeout=30000)
        assert sections.locator("details").count() == 4
        assert "queue live" in board_el.locator(".awm-heartbeat").inner_text()

        def in_section(key: str, task_id: str):
            return sections.locator(f"details[data-section={key}] .awm-card[data-task='{task_id}']")

        in_section("proposed", proposed).wait_for()
        in_section("working", working).wait_for()
        assert in_section("done", done).count() == 1

        board_el.locator(".awm-search").fill(f"Vault backup {run}")
        wait_for(lambda: sections.locator(".awm-card").count() == 1)
        board_el.locator(".awm-search").fill("")
        in_section("proposed", proposed).wait_for()

        in_section("working", working).click()
        info = board_el.locator(".awm-info")
        info.locator("a", has_text="Remote Control").wait_for()
        assert info.locator("a").get_attribute("href") == "https://claude.ai/code/session_w1"

        # A Proposed card: the panel shows the body's first lines and signs its hash.
        frame = board_el.locator("iframe.awm-panel").content_frame
        in_section("proposed", proposed).click()
        frame.locator("#card h2", has_text=f"Tunnel rework {run}").wait_for(timeout=15000)
        assert "Move the tunnel" in frame.locator("#card blockquote").inner_text()
        frame.locator("select.peer").first.select_option("deneb")
        frame.locator("button", has_text="Spawn agent").click()
        try:
            wait_for(lambda: store.task_get(proposed)["section"] == "working")
        except AssertionError:
            raise AssertionError(frame.locator("#status").inner_text()) from None
        env = srv.call(ap.order_pull("deneb"))[0]
        assert env["order"]["payload"]["body"] == body
        assert env["order"]["payload"]["body_sha256"] == hashlib.sha256(body.encode()).hexdigest()

        writer.step()
        in_section("working", proposed).wait_for(state="attached", timeout=30000)
        assert not errors, errors
        browser.close()
