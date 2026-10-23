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
        # Every message in a chat, replies included (a reply carries thread_ts).
        self.history = {"D1": [{"ts": "100.000001", "user": "U2", "text": "old"}]}
        self.page = 200         # messages per page of a paged method
        self.connects = 0
        self.drop_first = None  # an Event: set it to close the first socket
        self.second_push = []   # envelopes the second socket sends after hello
        self.base = ""
        self._runner = None

    def _page(self, key, items, form):
        start = int(form.get("cursor") or 0)
        body = {"ok": True, key: items[start:start + self.page]}
        if start + self.page < len(items):
            body["response_metadata"] = {"next_cursor": str(start + self.page)}
        return web.json_response(body)

    def _top_level(self, channel, since):
        msgs = self.history.get(channel, [])
        out = []
        for m in msgs:
            if m.get("thread_ts", m["ts"]) != m["ts"] or float(m["ts"]) <= since:
                continue
            replies = [r["ts"] for r in msgs
                       if r.get("thread_ts") == m["ts"] and r["ts"] != m["ts"]]
            if replies:
                m = {**m, "thread_ts": m["ts"], "reply_count": len(replies),
                     "latest_reply": max(replies, key=float)}
            out.append(m)
        return sorted(out, key=lambda m: float(m["ts"]), reverse=True)

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
            if "oldest" in form and float(form["oldest"]) <= 0:
                return web.json_response({"ok": False, "error": "invalid_ts_oldest"})
            since = float(form.get("oldest") or 0)
            return self._page("messages", self._top_level(form["channel"], since), form)
        if method == "conversations.replies":
            since = float(form.get("oldest") or 0)
            thread = [m for m in self.history.get(form["channel"], [])
                      if m["ts"] == form["ts"] or m.get("thread_ts") == form["ts"]]
            thread.sort(key=lambda m: float(m["ts"]))
            replies = [m for m in thread[1:] if float(m["ts"]) > since]
            return self._page("messages", thread[:1] + replies, form)
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
        self.connects += 1
        await ws.send_json({"type": "hello"})
        for env in self.push if self.connects == 1 else self.second_push:
            await ws.send_json(env)
        if self.drop_first is not None and self.connects == 1:
            await self.drop_first.wait()
            await ws.close()
            return ws
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


def _connector(fake, store):
    from awm.social.connectors.base import Account
    from awm.social.connectors.slack_bot_conn import SlackBotConnector
    from awm.social.history import History

    async def on_message(m):
        pass

    conn = SlackBotConnector(
        Account(name="bot", platform="slack", token="xoxb-1", app_token="xapp-1"),
        on_message)
    conn.api_base = fake.base + "/api/"
    conn.history = History(store, "bot")
    return conn


async def test_live_event_after_reconnect_never_skips_the_gap(awm_workspace):
    """A live event handled before the reconnect sync leaves the cursor alone."""
    from awm.social import dao

    dao.init()
    store = dao.SocialDAO()
    fake = _FakeSlack()
    fake.drop_first = asyncio.Event()
    await fake.start()
    conn = _connector(fake, store)
    task = asyncio.create_task(conn.start())
    try:
        await _until(lambda: fake.connects == 1
                     and store.synced_ts("bot", "D1") == "100.000001")
        # While the socket is down: one message nobody pushes, then a live one
        # that the second socket delivers right behind its hello.
        fake.history["D1"].append({"ts": "150.000001", "user": "U2", "text": "missed"})
        fake.history["D1"].append({"ts": "200.000001", "user": "U2", "text": "live"})
        fake.second_push = [_event("e9", user="U2", text="live", ts="200.000001")]
        fake.drop_first.set()
        await _until(lambda: fake.connects == 2 and fake.acks == ["e9"], timeout=8)
        await _until(lambda: store.synced_ts("bot", "D1") == "200.000001")
    finally:
        task.cancel()
        await conn.close()
        await fake.stop()
    assert [r["text"] for r in store.history("bot", channel_id="D1")] == [
        "old", "missed", "live"]


async def test_sync_pages_and_catches_replies_on_old_threads(awm_workspace):
    """Replies posted while away reach history, even under a thread far older
    than the cursor, as long as the thread was active in the window."""
    from awm.social import dao
    from awm.social.connectors.slack_bot_conn import THREAD_WINDOW_S

    dao.init()
    store = dao.SocialDAO()
    fake = _FakeSlack()
    fake.page = 2
    old = 1_000_000.0                       # a parent far below the window
    cursor = old + 2 * THREAD_WINDOW_S
    recent = cursor - 3600                  # a parent inside the window
    ts = lambda x: f"{x:.6f}"  # noqa: E731
    fake.history["D1"] = [
        {"ts": ts(old), "user": "U2", "text": "old parent"},
        {"ts": ts(cursor - 60), "user": "U2", "text": "old-thread reply seen",
         "thread_ts": ts(old)},
        {"ts": ts(recent), "user": "U2", "text": "recent parent"},
        {"ts": ts(cursor), "user": "U2", "text": "last seen"},
        # Everything below arrived while the socket was down.
        {"ts": ts(cursor + 1), "user": "U2", "text": "old-thread reply missed",
         "thread_ts": ts(old)},
        {"ts": ts(cursor + 2), "user": "U2", "text": "recent-thread reply",
         "thread_ts": ts(recent)},
    ] + [{"ts": ts(cursor + 10 + i), "user": "U2", "text": f"new {i}"}
         for i in range(5)]
    store.upsert_message("bot", {"channel_id": "D1", "message_id": ts(cursor - 60),
                                 "thread_id": ts(old), "ts": ts(cursor - 60),
                                 "text": "old-thread reply seen"})
    store.set_synced("bot", "D1", ts(cursor))

    await fake.start()
    conn = _connector(fake, store)
    try:
        await conn.sync()
    finally:
        await conn.close()
        await fake.stop()

    texts = [r["text"] for r in store.history("bot", channel_id="D1", since=ts(cursor))]
    assert texts == ["old-thread reply missed", "recent-thread reply",
                     "new 0", "new 1", "new 2", "new 3", "new 4"]
    assert store.synced_ts("bot", "D1") == ts(cursor + 14)


async def test_a_chat_first_synced_empty_syncs_once_it_has_messages(awm_workspace):
    from awm.social import dao

    dao.init()
    store = dao.SocialDAO()
    fake = _FakeSlack()
    store.set_synced("bot", "D1", "0")

    await fake.start()
    conn = _connector(fake, store)
    try:
        await conn.sync()
    finally:
        await conn.close()
        await fake.stop()

    assert [r["text"] for r in store.history("bot", channel_id="D1")] == ["old"]
    assert store.synced_ts("bot", "D1") == "100.000001"
