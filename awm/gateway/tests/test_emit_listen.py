"""``awm services listen``: an emit topic becomes filtered lines on stdout."""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.unit]

import asyncio
import functools
import json

import websockets

from awm import gatewayclient
from awm.gateway import emit_listen


async def test_listen_filters_and_announces_a_reconnect(monkeypatch):
    connections = 0
    paths: list[str] = []

    async def stub(ws):
        nonlocal connections
        connections += 1
        paths.append(ws.request.path)
        if connections == 1:
            await ws.send(json.dumps({"type": "owner.said", "text": "line one\nline two"}))
            await ws.send(json.dumps({"type": "task.output", "data": "noise"}))
            await ws.send(json.dumps({"type": "session.phase", "phase": "cut"}))
            await ws.close(code=1012)
        else:
            await ws.send(json.dumps({"type": "owner.said", "text": "after"}))
            await ws.wait_closed()

    lines: list[str] = []
    async with websockets.serve(stub, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        monkeypatch.setenv("AWM_HUB_URL", f"http://127.0.0.1:{port}")
        stream = functools.partial(gatewayclient.subscribe, "tether", "session")
        task = asyncio.create_task(emit_listen.listen(
            stream, ["owner.", "session."], lines.append, backoff_s=0.05))
        for _ in range(200):
            if len(lines) >= 4:
                break
            await asyncio.sleep(0.01)
        task.cancel()

    assert paths[0] == "/svc/tether/emit/session"
    assert all("\n" not in line for line in lines)
    assert [json.loads(line)["type"] for line in lines] == [
        "owner.said", "session.phase", "listen.reconnected", "owner.said"]
    assert json.loads(lines[0])["text"] == "line one\nline two"


def test_empty_match_passes_everything():
    assert emit_listen.matches({"type": "task.output"}, ())
    assert not emit_listen.matches({"type": "task.output"}, ("owner.",))
    assert not emit_listen.matches(["not", "an", "event"], ("owner.",))
