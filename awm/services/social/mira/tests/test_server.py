"""Tests for the daemon's auth middleware and inbound-watcher logic."""

from __future__ import annotations

import pytest

from mira_api.server import MiraAPI, Platform

pytestmark = pytest.mark.asyncio


# --------------------------------------------------------------------------- #
# auth middleware                                                              #
# --------------------------------------------------------------------------- #

class _FakeReq:
    def __init__(self, auth: str | None):
        self.headers = {"Authorization": auth} if auth is not None else {}


async def _handler(_req):
    return "OK"


class TestAuth:
    async def test_rejects_missing_token(self):
        api = MiraAPI("http://x", [], "secret")
        resp = await api._auth_mw(_FakeReq(None), _handler)
        assert resp.status == 401

    async def test_rejects_wrong_token(self):
        api = MiraAPI("http://x", [], "secret")
        resp = await api._auth_mw(_FakeReq("Bearer nope"), _handler)
        assert resp.status == 401

    async def test_accepts_correct_token(self):
        api = MiraAPI("http://x", [], "secret")
        out = await api._auth_mw(_FakeReq("Bearer secret"), _handler)
        assert out == "OK"


# --------------------------------------------------------------------------- #
# inbound watcher                                                              #
# --------------------------------------------------------------------------- #

class _FakeDriver:
    """Returns a scripted newest-first message list per fetch call."""

    platform = "slack"

    def __init__(self):
        self.script: list[dict] = []
        self.last_fetch: dict | None = None

    async def identity(self):
        return {"id": "ME", "name": "me"}

    async def list_conversations(self):
        return [{"id": "C1", "name": "general", "kind": "channel"}]

    async def fetch_messages(self, channel, oldest, limit, *, before=None):
        self.last_fetch = {"channel": channel, "oldest": oldest,
                           "limit": limit, "before": before}
        return self.script[:limit]


def _msg(marker, sender, text):
    return {"platform": "slack", "channel_id": "C1", "channel_name": "",
            "message_id": marker, "marker": marker, "sender_id": sender,
            "sender_name": sender, "text": text, "ts": marker, "thread_id": ""}


class TestWatcher:
    async def test_baseline_then_delta_with_filtering(self):
        api = MiraAPI("http://x", [], "secret", poll_interval=0.01)
        pushed: list[dict] = []

        async def _capture(payload):
            pushed.append(payload)

        api._broadcast = _capture
        drv = _FakeDriver()
        p = Platform("slack", page=None, driver=drv)
        p.self_id = "ME"
        api._platforms["slack"] = p

        # Round 1 — baseline to the latest marker; nothing is replayed.
        drv.script = [_msg("100", "U2", "old")]
        await api._poll_conversation(p, {"id": "C1", "name": "general"})
        assert pushed == []
        assert p.baseline["C1"] == "100"

        # Round 2 — newest-first: a system-less own echo + a real inbound, both
        # newer than the baseline; only the foreign real message is emitted.
        drv.script = [
            _msg("102", "U2", "hi tony"),
            _msg("101", "ME", "my own send"),
            _msg("100", "U2", "old"),
        ]
        await api._poll_conversation(p, {"id": "C1", "name": "general"})
        texts = [f["message"]["text"] for f in pushed]
        assert texts == ["hi tony"]                 # own + boundary dropped
        assert p.baseline["C1"] == "102"            # advances past skipped echo
        assert pushed[0]["message"]["channel_name"] == "general"

    async def test_empty_conversation_baselines_blank(self):
        api = MiraAPI("http://x", [], "secret")
        api._broadcast = lambda payload: _aiopass()
        drv = _FakeDriver()
        drv.script = []
        p = Platform("slack", page=None, driver=drv)
        api._platforms["slack"] = p
        await api._poll_conversation(p, {"id": "C1"})
        assert p.baseline["C1"] == ""


async def _aiopass():
    return None


# --------------------------------------------------------------------------- #
# GET /v1/{platform}/messages (on-demand history)                             #
# --------------------------------------------------------------------------- #

class _MsgReq:
    """Minimal stand-in for an aiohttp request to _h_messages."""

    def __init__(self, platform, query):
        self.match_info = {"platform": platform}
        self.query = query


class TestMessagesEndpoint:
    async def test_forwards_before_and_limit_to_driver(self):
        import json

        api = MiraAPI("http://x", [], "secret")
        drv = _FakeDriver()
        drv.script = [_msg("9.2", "U2", "newer"), _msg("9.1", "U1", "older")]
        api._platforms["slack"] = Platform("slack", page=None, driver=drv)

        resp = await api._h_messages(
            _MsgReq("slack", {"channel": "C1", "limit": "10", "before": "9.5"}))
        assert resp.status == 200
        assert drv.last_fetch == {
            "channel": "C1", "oldest": None, "limit": 10, "before": "9.5"}
        body = json.loads(resp.text)
        assert [m["text"] for m in body["messages"]] == ["newer", "older"]

    async def test_before_optional(self):
        api = MiraAPI("http://x", [], "secret")
        drv = _FakeDriver()
        api._platforms["slack"] = Platform("slack", page=None, driver=drv)
        await api._h_messages(_MsgReq("slack", {"channel": "C1"}))
        assert drv.last_fetch["before"] is None

    async def test_channel_required(self):
        api = MiraAPI("http://x", [], "secret")
        api._platforms["slack"] = Platform(
            "slack", page=None, driver=_FakeDriver())
        resp = await api._h_messages(_MsgReq("slack", {}))
        assert resp.status == 400
