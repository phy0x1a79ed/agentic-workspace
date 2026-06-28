"""Tests for the MiraConnector thin client.

Exercises the connector against a *real* stub of the mira API daemon (an
aiohttp server on loopback): the REST send/channels/identity mapping, and the
WebSocket inbound path (frame → on_message, with platform + own-echo filtering).
"""

from __future__ import annotations

import asyncio

import pytest

pytestmark = [pytest.mark.unit]


async def _noop(_m):
    return None


# --------------------------------------------------------------------------- #
# build() routing                                                             #
# --------------------------------------------------------------------------- #

class TestBuildRouting:
    def test_source_mira_routes_slack_to_mira_connector(self):
        from awm.social import connectors
        from awm.social.config import AccountConfig
        from awm.social.connectors.mira_conn import MiraConnector

        c = connectors.build(
            AccountConfig(name="slack-via-mira", platform="slack", token="",
                          source="mira", mira_url="https://h:7822",
                          mira_token="tok"), _noop)
        assert isinstance(c, MiraConnector)
        assert c.platform == "slack"          # platform preserved for routing

    def test_source_mira_routes_teams_to_mira_connector(self):
        from awm.social import connectors
        from awm.social.config import AccountConfig
        from awm.social.connectors.mira_conn import MiraConnector

        c = connectors.build(
            AccountConfig(name="teams", platform="teams", token="",
                          source="mira", mira_url="https://h:7822",
                          mira_token="tok"), _noop)
        assert isinstance(c, MiraConnector)
        assert c.platform == "teams"

    def test_non_mira_slack_stays_native(self):
        from awm.social import connectors
        from awm.social.config import AccountConfig
        from awm.social.connectors.slack_conn import SlackConnector

        c = connectors.build(
            AccountConfig(name="s", platform="slack", token="xoxb-x",
                          app_token="xapp-1"), _noop)
        assert isinstance(c, SlackConnector)


# --------------------------------------------------------------------------- #
# Stub mira daemon + live REST/WS exercise                                      #
# --------------------------------------------------------------------------- #

class _StubDaemon:
    """A minimal aiohttp stand-in for the mira API daemon on loopback."""

    def __init__(self):
        self.sent = []
        self.push_frames = []   # frames the WS pushes right after connect
        self._runner = None
        self.base = ""

    async def start(self):
        from aiohttp import web

        app = web.Application()
        app.router.add_get("/v1/{platform}/identity", self._identity)
        app.router.add_get("/v1/{platform}/channels", self._channels)
        app.router.add_post("/v1/{platform}/send", self._send)
        app.router.add_get("/v1/events", self._events)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        port = list(self._runner.addresses)[0][1]
        self.base = f"http://127.0.0.1:{port}"

    async def stop(self):
        if self._runner:
            await self._runner.cleanup()

    async def _identity(self, request):
        from aiohttp import web
        return web.json_response({"id": "ME", "name": "me-on-mira"})

    async def _channels(self, request):
        from aiohttp import web
        return web.json_response({"channels": [
            {"id": "C1", "name": "general", "kind": "channel"},
            {"id": "D1", "name": "dm:bob", "kind": "dm"}]})

    async def _send(self, request):
        from aiohttp import web
        body = await request.json()
        self.sent.append((request.match_info["platform"], body))
        return web.json_response(
            {"ok": True, "message_id": "m-123", "channel_id": body["channel"],
             "ts": "1719.0"})

    async def _events(self, request):
        from aiohttp import web
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        await ws.send_json({"type": "ready", "platforms": ["slack", "teams"]})
        for frame in self.push_frames:
            await ws.send_json(frame)
        # keep open briefly so the client can process
        await asyncio.sleep(0.3)
        await ws.close()
        return ws


@pytest.fixture()
async def daemon():
    d = _StubDaemon()
    await d.start()
    yield d
    await d.stop()


def _conn(daemon, platform="slack", on_message=_noop):
    from awm.social.connectors.base import Account
    from awm.social.connectors.mira_conn import MiraConnector
    return MiraConnector(
        Account(name=f"{platform}-mira", platform=platform, token="",
                source="mira", mira_url=daemon.base, mira_token="tok",
                mira_verify_tls=False),
        on_message)


class TestMiraConnectorRest:
    async def test_identity(self, daemon):
        c = _conn(daemon)
        try:
            ident = await c.identity()
            assert ident.id == "ME" and ident.name == "me-on-mira"
        finally:
            await c.close()

    async def test_send_posts_to_platform_route(self, daemon):
        c = _conn(daemon, platform="teams")
        try:
            out = await c.send("19:chat@x", "hello there", thread="t1")
            assert out["message_id"] == "m-123"
            assert out["channel_id"] == "19:chat@x"
            platform, body = daemon.sent[-1]
            assert platform == "teams"
            assert body == {"channel": "19:chat@x", "text": "hello there", "thread": "t1"}
        finally:
            await c.close()

    async def test_list_channels(self, daemon):
        c = _conn(daemon)
        try:
            chans = await c.list_channels()
            assert {ch.id for ch in chans} == {"C1", "D1"}
            assert {ch.kind for ch in chans} == {"channel", "dm"}
        finally:
            await c.close()


class TestMiraConnectorInbound:
    async def test_ws_frame_forwarded_with_filtering(self, daemon):
        received = []

        async def on_msg(m):
            received.append(m)

        # one valid slack message, one own-echo (sender == self id ME), and one
        # wrong-platform frame — only the first should reach on_message.
        daemon.push_frames = [
            {"type": "message", "message": {
                "platform": "slack", "channel_id": "C1", "channel_name": "general",
                "sender_id": "U2", "sender_name": "bob", "message_id": "9.1",
                "ts": "9.1", "text": "hi tony", "thread_id": ""}},
            {"type": "message", "message": {
                "platform": "slack", "channel_id": "C1", "sender_id": "ME",
                "message_id": "9.2", "text": "my own echo"}},
            {"type": "message", "message": {
                "platform": "teams", "channel_id": "x", "sender_id": "U9",
                "message_id": "9.3", "text": "other platform"}},
        ]
        c = _conn(daemon, platform="slack", on_message=on_msg)
        task = asyncio.ensure_future(c.start())
        try:
            for _ in range(50):
                if received:
                    break
                await asyncio.sleep(0.05)
            assert len(received) == 1
            m = received[0]
            assert m.platform == "slack" and m.text == "hi tony"
            assert m.sender_name == "bob" and m.channel_id == "C1"
            assert m.account == "slack-mira"
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await c.close()
