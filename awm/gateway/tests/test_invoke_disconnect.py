"""A client that leaves ``/invoke`` cancels the call it was waiting on."""

from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException

from awm.gateway import server

pytestmark = [pytest.mark.unit]


class _Request:
    def __init__(self, gone_after: int):
        self.polls = 0
        self.gone_after = gone_after

    async def is_disconnected(self) -> bool:
        self.polls += 1
        return self.polls > self.gone_after


def test_a_disconnect_cancels_the_dispatch(monkeypatch):
    monkeypatch.setattr(server, "_DISCONNECT_POLL_S", 0.01)
    seen = {}

    async def never():
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            seen["cancelled"] = True
            raise

    async def go():
        with pytest.raises(HTTPException) as ei:
            await server._dispatch_while_connected(_Request(gone_after=2), never())
        await asyncio.sleep(0)
        return ei.value.status_code

    assert asyncio.run(go()) == 499
    assert seen == {"cancelled": True}


def test_a_connected_client_gets_the_result(monkeypatch):
    monkeypatch.setattr(server, "_DISCONNECT_POLL_S", 0.01)

    async def slow():
        await asyncio.sleep(0.05)
        return {"ok": 1}

    req = _Request(gone_after=10**6)
    assert asyncio.run(server._dispatch_while_connected(req, slow())) == {"ok": 1}
    assert req.polls >= 1
