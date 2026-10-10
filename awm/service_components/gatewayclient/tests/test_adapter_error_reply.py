"""A failed call replies with the error text and the exception class.

The gateway maps a foreign caller's ``PermissionError`` to the 404 an unknown
tool gets, and the class name is the only thing that lets it tell that apart
from any other service failure. Sync tests driving async work via ``asyncio.run``.
"""

from __future__ import annotations

import asyncio
import json

from awm.gatewayclient.adapter import ServiceAdapter


class _WS:
    def __init__(self):
        self.sent = []

    async def send(self, data):
        self.sent.append(json.loads(data))


def _reply(handler):
    ad = ServiceAdapter("svc", {"functions": [{"name": "f"}]}, {"f": handler})
    ws = _WS()
    asyncio.run(ad._handle_call(ws, {"kind": "call", "id": "1", "fn": "f", "args": {}}))
    return ws.sent[0]


def _refuse(args):
    raise PermissionError("goals are not readable by peers")


def _break(args):
    raise KeyError("missing")


def test_an_error_reply_names_the_exception_class():
    assert _reply(_refuse) == {"kind": "reply", "id": "1", "ok": False,
                               "error": "goals are not readable by peers",
                               "error_class": "PermissionError"}
    assert _reply(_break)["error_class"] == "KeyError"


def test_an_ok_reply_is_unchanged():
    assert _reply(lambda args: {"x": 1}) == {"kind": "reply", "id": "1", "ok": True, "result": {"x": 1}}
