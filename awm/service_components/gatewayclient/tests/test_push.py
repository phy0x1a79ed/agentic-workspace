"""The R69 push loop that social's seats and the channel plugin share."""

from __future__ import annotations

import asyncio

from awm.gatewayclient import push


async def test_pusher_delivers_in_order_and_retries_a_refusal():
    sent, outcomes = [], iter(["retry", "ok", "ok", "ok"])
    slept = []

    async def send(node, job, line):
        sent.append(line)
        return next(outcomes)

    async def sleep(s):
        slept.append(s)

    p = push.Pusher(send=send, sleep=sleep)
    for line in ("a", "b", "c"):
        p.push("altair", "j1", line)
    await p.idle()
    assert sent == ["a", "a", "b", "c"] and slept == [2.0]


async def test_pusher_drops_after_the_retry_window_and_releases_a_gone_holder():
    gone = []

    async def refuse(node, job, line):
        return "retry"

    p = push.Pusher(send=refuse, retry_s=0.05, sleep=lambda s: asyncio.sleep(0))
    p.push("altair", "j1", "a")
    await p.idle()

    sent = []

    async def vanish(node, job, line):
        sent.append(line)
        return "gone"

    p = push.Pusher(send=vanish, on_gone=lambda n, j: gone.append((n, j)))
    p.push("altair", "j1", "a")
    p.push("altair", "j1", "b")
    await p.idle()
    assert sent == ["a"] and gone == [("altair", "j1")]


async def test_a_session_still_writing_its_record_is_retried_not_released(monkeypatch):
    """A just-resumed REPL refuses pushes for a few seconds after a reboot."""

    async def refuse(*a, **kw):
        return {"ok": False, "error": "no Claude Code session record for pid 6595"}

    monkeypatch.setattr(push.config, "node_name", lambda: "deneb")
    monkeypatch.setattr(push, "call", refuse)
    assert await push.reflect("deneb", "f97d97d7", "hello") == "retry"
