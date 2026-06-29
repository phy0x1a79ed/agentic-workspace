"""Tests for the per-platform drivers' Python normalisation layer.

The in-page JavaScript is verified live on mira (it must run against the real
logged-in sessions); these tests cover the Python side — how each driver maps
the JS return value into the normalised message/channel shape, which helper it
invokes, and the args it passes — using a fake CDP page.
"""

from __future__ import annotations

import json

import pytest

from mira_api.drivers import SlackDriver, TeamsDriver

pytestmark = pytest.mark.asyncio


class FakePage:
    """Stands in for CDPPage: returns canned data keyed by the helper called."""

    def __init__(self, responses: dict):
        self.responses = responses
        self.exprs: list[str] = []

    async def evaluate_json(self, expr: str):
        self.exprs.append(expr)
        for helper, value in self.responses.items():
            if helper in expr:
                return value
        return None


class TestSlackDriver:
    async def test_fetch_messages_filters_subtypes_and_normalises(self):
        page = FakePage({"window.__awmMira.slack.history": [
            {"ts": "3.0", "user": "U1", "text": "hi", "thread_ts": "2.0"},
            {"ts": "2.5", "user": "U2", "text": "joined", "subtype": "channel_join"},
        ]})
        msgs = await SlackDriver(page).fetch_messages("C1", None, 50)
        assert len(msgs) == 1
        m = msgs[0]
        assert m["platform"] == "slack" and m["channel_id"] == "C1"
        assert m["text"] == "hi" and m["marker"] == "3.0" == m["message_id"]
        assert m["sender_id"] == "U1" and m["thread_id"] == "2.0"

    async def test_fetch_messages_passes_oldest_arg(self):
        page = FakePage({"window.__awmMira.slack.history": []})
        await SlackDriver(page).fetch_messages("C1", "100.0", 50)
        # the oldest marker is threaded into the helper call as a JSON arg
        assert '"100.0"' in page.exprs[-1]

    async def test_fetch_messages_passes_before_as_latest(self):
        page = FakePage({"window.__awmMira.slack.history": []})
        await SlackDriver(page).fetch_messages("C1", None, 50, before="9.5")
        # before is threaded as the 4th JS arg (mapped to Slack's `latest`).
        assert '"9.5"' in page.exprs[-1]

    async def test_send_returns_helper_payload(self):
        page = FakePage({"window.__awmMira.slack.send":
                         {"message_id": "1.2", "channel_id": "C1", "ts": "1.2"}})
        out = await SlackDriver(page).send("C1", "yo")
        assert out["message_id"] == "1.2"

    async def test_list_channels_passthrough(self):
        page = FakePage({"window.__awmMira.slack.listChannels":
                         [{"id": "C1", "name": "general", "kind": "channel"}]})
        chans = await SlackDriver(page).list_channels()
        assert chans[0]["id"] == "C1"


class TestTeamsDriver:
    async def test_fetch_messages_normalises(self):
        page = FakePage({"window.__awmMira.teams.messages": [
            {"message_id": "1700000000002", "marker": "1700000000002",
             "channel_id": "19:abc@unq.gbl.spaces", "sender_id": "8:orgid:u2",
             "sender_name": "Bob", "text": "hello", "ts": "2026-06-28T00:00:00Z",
             "thread_id": ""}]})
        msgs = await TeamsDriver(page).fetch_messages("19:abc@unq.gbl.spaces", None, 20)
        assert len(msgs) == 1
        m = msgs[0]
        assert m["platform"] == "teams"
        assert m["sender_name"] == "Bob" and m["text"] == "hello"
        assert m["marker"] == "1700000000002" == m["message_id"]

    async def test_fetch_messages_ignores_before(self):
        # Teams has no usable cursor; `before` must be silently dropped, not
        # threaded into the helper call (which only ever gets channel + limit).
        page = FakePage({"window.__awmMira.teams.messages": []})
        await TeamsDriver(page).fetch_messages("19:abc", None, 20, before="9.5")
        assert "19:abc" in page.exprs[-1]
        assert '"9.5"' not in page.exprs[-1]

    async def test_list_conversations_passthrough(self):
        page = FakePage({"window.__awmMira.teams.listConversations":
                         [{"id": "19:abc@unq.gbl.spaces", "name": "", "kind": "dm"}]})
        convs = await TeamsDriver(page).list_conversations()
        assert convs[0]["kind"] == "dm"

    async def test_send_threads_args(self):
        page = FakePage({"window.__awmMira.teams.send":
                         {"message_id": "x", "channel_id": "19:abc", "ts": ""}})
        await TeamsDriver(page).send("19:abc", "hi", thread=None)
        expr = page.exprs[-1]
        assert "19:abc" in expr and json.dumps("hi") in expr
