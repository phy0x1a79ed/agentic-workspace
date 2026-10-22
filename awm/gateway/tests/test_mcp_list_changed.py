"""Both MCP proxies tell the client when the tool list it was served goes stale.

A session that starts before the gateway's first peer sweep is served a list
without the peer tools. Claude Code re-lists only when told to, so without the
notice that session never sees them.
"""

import asyncio
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

pytestmark = [pytest.mark.smoke, pytest.mark.mcp]

from awm.gateway import mcp_http


def _tool(name):
    return {"name": name, "description": name, "inputSchema": {"type": "object"}}


@pytest.fixture()
def core():
    """A core whose ``GET /tools`` serves whatever ``state["tools"]`` holds."""
    state = {"tools": [_tool("scope")]}

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            body = json.dumps({"tools": state["tools"]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    state["url"] = f"http://127.0.0.1:{srv.server_address[1]}"
    yield state
    srv.shutdown()


@pytest.fixture()
def fast_watch(monkeypatch):
    monkeypatch.setattr(mcp_http, "WATCH_FAST_S", 0.05)


def _wait_for(pred, timeout=3.0):
    deadline = time.monotonic() + timeout
    while not pred() and time.monotonic() < deadline:
        time.sleep(0.02)
    return pred()


_CHANGED = {"jsonrpc": "2.0", "method": "notifications/tools/list_changed"}


class TestToolListWatch:
    def test_nothing_to_compare_before_a_list_is_served(self):
        assert not mcp_http.ToolListWatch().changed([_tool("a")])

    def test_each_new_list_is_announced_once(self):
        w = mcp_http.ToolListWatch()
        w.served([_tool("a")])
        assert not w.changed([_tool("a")])
        assert w.changed([_tool("a"), _tool("social")])
        assert not w.changed([_tool("a"), _tool("social")])

    def test_a_fresh_list_rearms_it(self):
        w = mcp_http.ToolListWatch()
        w.served([_tool("a")])
        assert w.changed([_tool("b")])
        w.served([_tool("b")])
        assert not w.changed([_tool("b")])
        assert w.changed([_tool("a")])


class TestStdioProxy:
    @pytest.fixture()
    def proxy(self, core, fast_watch, monkeypatch):
        from awm.gateway import mcp_stdio
        monkeypatch.setattr(mcp_stdio.config, "BASE_URL", core["url"])
        monkeypatch.setattr(mcp_stdio, "_ensure_core_running", lambda: None)
        monkeypatch.setattr(mcp_stdio, "_watch", mcp_http.ToolListWatch())
        monkeypatch.setattr(mcp_stdio, "_watcher", None)
        stop = threading.Event()
        monkeypatch.setattr(mcp_stdio, "_watcher_stop", stop)
        sent = []
        monkeypatch.setattr(mcp_stdio, "_send", sent.append)
        yield mcp_stdio, sent
        stop.set()
        if mcp_stdio._watcher is not None:
            mcp_stdio._watcher.join(2)

    def test_initialize_declares_list_changed(self):
        from awm.gateway import mcp_stdio
        caps = mcp_stdio._handle_initialize({})["capabilities"]
        assert caps["tools"]["listChanged"] is True

    def test_notifies_once_when_the_list_grows(self, proxy, core):
        mcp_stdio, sent = proxy
        mcp_stdio._dispatch({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        mcp_stdio._dispatch({"jsonrpc": "2.0", "method": "notifications/initialized"})
        time.sleep(0.3)
        assert _CHANGED not in sent, "a stable list sends nothing"

        core["tools"] = [_tool("scope"), _tool("social")]
        assert _wait_for(lambda: _CHANGED in sent)
        time.sleep(0.3)
        assert sent.count(_CHANGED) == 1

    def test_nothing_is_sent_before_initialized(self, proxy, core):
        mcp_stdio, sent = proxy
        mcp_stdio._dispatch({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        core["tools"] = [_tool("scope"), _tool("social")]
        time.sleep(0.3)
        assert mcp_stdio._watcher is None
        assert _CHANGED not in sent

    def test_a_down_core_is_swallowed(self, proxy, core, monkeypatch):
        mcp_stdio, sent = proxy
        mcp_stdio._dispatch({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        good = core["url"]
        monkeypatch.setattr(mcp_stdio.config, "BASE_URL", "http://127.0.0.1:9")
        mcp_stdio._dispatch({"jsonrpc": "2.0", "method": "notifications/initialized"})
        time.sleep(0.3)
        assert mcp_stdio._watcher.is_alive()

        core["tools"] = [_tool("scope"), _tool("social")]
        monkeypatch.setattr(mcp_stdio.config, "BASE_URL", good)
        assert _wait_for(lambda: _CHANGED in sent, timeout=5)


class TestSdkProxy:
    def test_initialize_declares_list_changed(self):
        from mcp.server import NotificationOptions

        from awm.gateway import mcp_server_sdk
        opts = mcp_server_sdk.server.create_initialization_options(
            NotificationOptions(tools_changed=True))
        assert opts.capabilities.tools.listChanged is True

    def test_notifies_once_when_the_list_grows(self, core, fast_watch, monkeypatch):
        from awm.gateway import mcp_server_sdk
        monkeypatch.setattr(mcp_server_sdk.config, "BASE_URL", core["url"])
        monkeypatch.setattr(mcp_server_sdk, "_watch", mcp_http.ToolListWatch())

        class Session:
            sent = 0

            async def send_tool_list_changed(self):
                Session.sent += 1

        async def go():
            mcp_server_sdk._watch.served(
                (await mcp_server_sdk._fetch_tools())["tools"])
            task = asyncio.create_task(mcp_server_sdk._watch_tool_list(Session()))
            await asyncio.sleep(0.3)
            assert Session.sent == 0
            core["tools"] = [_tool("scope"), _tool("social")]
            await asyncio.sleep(0.4)
            task.cancel()

        asyncio.run(go())
        assert Session.sent == 1
