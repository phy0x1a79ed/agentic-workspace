"""``awm services listen``: an emit topic becomes filtered lines on stdout."""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.unit]

import asyncio
import json

import websockets

from awm.gateway import emit_listen


async def test_listen_filters_and_announces_a_reconnect():
    connections = 0

    async def stub(ws):
        nonlocal connections
        connections += 1
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
        task = asyncio.create_task(emit_listen.listen(
            f"ws://127.0.0.1:{port}/svc/tether/emit/session",
            ["owner.", "session."], lines.append, backoff_s=0.05))
        for _ in range(200):
            if len(lines) >= 4:
                break
            await asyncio.sleep(0.01)
        task.cancel()

    assert all("\n" not in line for line in lines)
    assert [json.loads(line)["type"] for line in lines] == [
        "owner.said", "session.phase", "listen.reconnected", "owner.said"]
    assert json.loads(lines[0])["text"] == "line one\nline two"


def test_empty_match_passes_everything():
    assert emit_listen.matches({"type": "task.output"}, ())
    assert not emit_listen.matches({"type": "task.output"}, ("owner.",))
    assert not emit_listen.matches(["not", "an", "event"], ("owner.",))
