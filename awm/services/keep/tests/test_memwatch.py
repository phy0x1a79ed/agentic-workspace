"""The low-memory warning, on a fake clock."""

from __future__ import annotations

import asyncio
import os

from awm.keep import memwatch
from awm.keep.memwatch import ALERT_EVERY_S, SUSTAIN_S, Hog, MemWatch

GB = 1024 * 1024
T0 = 1_792_000_000.0


def test_meminfo_is_parsed_in_kb():
    text = "MemTotal:       61765432 kB\nMemFree:  1 kB\nMemAvailable:    8123456 kB\n"
    assert memwatch.parse_meminfo(text) == (61765432, 8123456)


def test_a_brief_dip_stays_quiet():
    w = MemWatch()
    assert not w.tick(T0, 60 * GB, 2 * GB)
    assert not w.tick(T0 + SUSTAIN_S - 1, 60 * GB, 2 * GB)
    assert not w.tick(T0 + SUSTAIN_S + 30, 60 * GB, 30 * GB)
    assert not w.tick(T0 + SUSTAIN_S + 60, 60 * GB, 2 * GB), "the clock restarts"


def test_low_memory_that_stays_low_warns_once_an_hour():
    w = MemWatch()
    w.tick(T0, 60 * GB, 2 * GB)
    assert w.tick(T0 + SUSTAIN_S, 60 * GB, 2 * GB)
    assert not w.tick(T0 + SUSTAIN_S + 30, 60 * GB, 2 * GB)
    assert w.tick(T0 + SUSTAIN_S + ALERT_EVERY_S, 60 * GB, 2 * GB)


def test_each_session_is_warned_once_an_hour():
    w = MemWatch()
    hogs = [Hog(1, 25 * GB, "python lb1.py", job="c63d29b9", session="lit kb"),
            Hog(2, 1 * GB, "python -m kb.server")]
    assert [h.job for h in w.jobs_to_warn(T0, hogs)] == ["c63d29b9"]
    assert w.jobs_to_warn(T0 + 60, hogs) == []
    assert [h.job for h in w.jobs_to_warn(T0 + ALERT_EVERY_S, hogs)] == ["c63d29b9"]


def test_the_notice_names_the_hog_and_its_session():
    text = memwatch.render("altair", 60 * GB, 2 * GB, [
        Hog(1572941, int(25.6 * GB), "python lb1.py", job="c63d29b9", session="lit kb")])
    assert "2.0 GB of 60.0 GB available" in text
    assert "pid 1572941, 25.6 GB: python lb1.py, in session c63d29b9 (lit kb)" in text


def test_this_process_is_found_among_the_hogs():
    hogs = memwatch.top_hogs(n=500)
    assert any(h.pid == os.getpid() and h.rss_kb > 0 for h in hogs)


def test_a_tick_tells_the_operator_and_the_sessions(monkeypatch):
    from awm.keep import hub_adapter

    sent, messaged = [], []
    monkeypatch.setattr(hub_adapter, "WATCH", MemWatch())
    monkeypatch.setattr(memwatch, "read_meminfo", lambda: (60 * GB, 2 * GB))
    monkeypatch.setattr(memwatch, "top_hogs", lambda: [
        Hog(7, 25 * GB, "python lb1.py", job="c63d29b9", session="lit kb")])
    monkeypatch.setattr(hub_adapter.config, "operator_dm", lambda: ("discord-bot", "D1"))
    monkeypatch.setattr(hub_adapter.config, "node_name", lambda: "altair")

    async def call_maybe_peer(peer, service, fn, args, **kw):
        sent.append((service, fn, args))

    async def call(service, fn, args, **kw):
        messaged.append((service, fn, args))

    monkeypatch.setattr(hub_adapter.gatewayclient, "call_maybe_peer", call_maybe_peer)
    monkeypatch.setattr(hub_adapter.gatewayclient, "call", call)
    asyncio.run(hub_adapter.memory_tick(T0))
    asyncio.run(hub_adapter.memory_tick(T0 + SUSTAIN_S))
    asyncio.run(hub_adapter.memory_tick(T0 + SUSTAIN_S + 30))
    ((service, fn, args),) = sent
    assert (service, fn, args["channel"]) == ("social", "send", "D1")
    assert "lb1.py" in args["text"]
    ((service, fn, args),) = messaged
    assert (service, fn, args["job"]) == ("reflection", "message", "c63d29b9")
