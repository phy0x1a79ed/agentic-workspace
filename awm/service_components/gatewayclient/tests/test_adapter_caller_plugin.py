"""A handler reads the calling plugin as ``_caller_plugin``, set from the call frame alone."""

from __future__ import annotations

import asyncio
import json

from awm.gatewayclient.adapter import PLUGIN_KEY, ServiceAdapter


class _WS:
    def __init__(self):
        self.sent = []

    async def send(self, data):
        self.sent.append(json.loads(data))


def _seen(frame: dict) -> dict:
    ad = ServiceAdapter("svc", {"functions": [{"name": "f"}]}, {"f": lambda args: args})
    ws = _WS()
    asyncio.run(ad._handle_call(ws, {"kind": "call", "id": "1", "fn": "f", **frame}))
    return ws.sent[0]["result"]


def test_the_frame_names_the_calling_plugin():
    assert _seen({"args": {"x": 1}, "plugin": "httpsfront"}) == {"x": 1, PLUGIN_KEY: "httpsfront"}


def test_a_call_with_no_plugin_carries_none():
    assert _seen({"args": {"x": 1}}) == {"x": 1}


def test_a_caller_cannot_claim_a_plugin_in_its_args():
    assert _seen({"args": {PLUGIN_KEY: "httpsfront"}}) == {}
    assert _seen({"args": {PLUGIN_KEY: "httpsfront"}, "plugin": "search"}) == {PLUGIN_KEY: "search"}
