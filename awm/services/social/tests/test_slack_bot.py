"""SlackBotConnector against a loopback stand-in for Slack (Web API + Socket Mode)."""

from __future__ import annotations

import asyncio

import pytest
from aiohttp import web

pytestmark = [pytest.mark.unit]


class _FakeSlack:
    def __init__(self):
        self.posted = []
        self.acks = []
        self.push = []          # envelopes the socket sends after hello
        self.history = {"D1": [{"ts": "100.000001", "user": "U2", "text": "old"}]}
        self.base = ""
        self._runner = None

    async def _api(self, request):
        method = request.match_info["method"]
        form = dict(await request.post())
        if method == "auth.test":
            return web.json_response({"ok": True, "user_id": "UBOT", "bot_id": "BBOT",
                                      "user": "awm", "team": "T"})
        if method == "apps.connections.open":
            assert request.headers["Authorization"] == "Bearer xapp-1"
            return web.json_response({"ok": True, "url": self.base.replace("http", "ws") + "/ws"})
        if method == "users.conversations":
            return web.json_response({"ok": True, "channels": [
                {"id": "D1", "is_im": True, "user": "U2"}]})
        if method == "conversations.history":
            since = float(form.get("oldest") or 0)
            msgs = [m for m in self.history.get(form["channel"], [])
                    if float(m["ts"]) > since]
            return web.json_response({"ok": True, "messages": msgs})
        if method == "users.info":
            return web.json_response({"ok": True, "user": {
                "id": form["user"], "profile": {"display_name": "tony"}}})
        if method == "chat.postMessage":
            self.posted.append(form)
            return web.json_response({"ok": True, "channel": "D1", "ts": "300.000001",
                                      "message": {"ts": "300.000001", "user": "UBOT",
                                                  "bot_id": "BBOT", "text": form["text"]}})
        return web.json_response({"ok": False, "error": "unknown_method"})

    async def _ws(self, request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        await ws.send_json({"type": "hello"})
        for env in self.push:
            await ws.send_json(env)
        async for frame in ws:
            self.acks.append(frame.json()["envelope_id"])
        return ws

    async def start(self):
        app = web.Application()
        app.router.add_post("/api/{method}", self._api)
        app.router.add_get("/ws", self._ws)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        self.base = f"http://127.0.0.1:{port}"

    async def stop(self):
        await self._runner.cleanup()


def _event(eid, **event):
    return {"type": "events_api", "envelope_id": eid,
            "payload": {"event": {"type": "message", "channel": "D1", **event}}}


async def _until(cond, timeout=5.0):
    for _ in range(int(timeout / 0.02)):
        if cond():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition not met")


async def test_receive_sync_and_send(awm_workspace):
    from awm.social import dao
    from awm.social.connectors.base import Account
    from awm.social.connectors.slack_bot_conn import SlackBotConnector
    from awm.social.history import History

    dao.init()
    store = dao.SocialDAO()
    fake = _FakeSlack()
    fake.push = [
        _event("e1", user="U2", text="hi bot", ts="200.000001"),
        _event("e2", user="UBOT", bot_id="BBOT", text="my own echo", ts="200.000002"),
        _event("e3", subtype="message_changed",
               message={"user": "U2", "text": "hi bot (edited)", "ts": "200.000001"}),
    ]
    await fake.start()
    got = []

    async def on_message(m):
        got.append(m)

    conn = SlackBotConnector(
        Account(name="bot", platform="slack", token="xoxb-1", app_token="xapp-1"),
        on_message)
    conn.api_base = fake.base + "/api/"
    conn.history = History(store, "bot")
    task = asyncio.create_task(conn.start())
    try:
        await _until(lambda: len(fake.acks) == 3)
        await _until(lambda: store.synced_ts("bot", "D1") is not None)
        sent = await conn.send("U2", "hello back")
    finally:
        task.cancel()
        await conn.close()
        await fake.stop()

    # Push: only the other person's message is emitted, never the bot's echo.
    assert [m.text for m in got] == ["hi bot"]
    assert got[0].sender_name == "tony"
    assert sent["channel_id"] == "D1" and fake.posted[0]["channel"] == "U2"
    # History: backfill + live + edit + own send, oldest first.
    rows = store.history("bot", channel_id="D1")
    assert [(r["text"], r["own"], r["edited"]) for r in rows] == [
        ("old", False, False),
        ("hi bot (edited)", False, True),
        ("my own echo", True, False),
        ("hello back", True, False),
    ]
    assert [r["text"] for r in store.history("bot", since="200.000001")] == [
        "my own echo", "hello back"]
