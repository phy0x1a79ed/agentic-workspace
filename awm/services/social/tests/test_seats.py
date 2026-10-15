"""Seats: mount, unmount and seats verbs, the pushed line, delivery and prune."""

from __future__ import annotations

import asyncio

import pytest

pytestmark = [pytest.mark.unit]

ME = {"kind": "bg", "job": "aaaa1111", "name": "opal desk"}
OTHER = {"kind": "bg", "job": "bbbb2222", "name": "other desk"}


def _msg(**kw):
    from awm.social.connectors.base import InboundMessage
    base = dict(account="bot", platform="slack", channel_id="D1", sender_id="U2",
                sender_name="tony", message_id="10.1", ts="10.1", text="hi")
    return InboundMessage(**{**base, **kw})


class _FakeConn:
    platform = "slack"
    keeps_history = True

    def __init__(self, seats, dms=()):
        from awm.social.connectors.base import Account, Channel
        self.account = Account(name="bot", platform=self.platform, token="x")
        self._seats = [Channel(id=i, name=n, kind=k) for i, n, k in seats]
        self._dms = [Channel(id=i, name=n, kind="dm") for i, n in dms]

    async def list_seats(self):
        return list(self._seats)

    async def list_channels(self, *, include_dms=False):
        return list(self._seats) + (list(self._dms) if include_dms else [])


class _FakePush:
    def __init__(self):
        self.lines = []

    def push(self, node, job, line):
        self.lines.append((node, job, line))


@pytest.fixture()
def wired(awm_workspace, monkeypatch):
    import awm.social.hub_adapter as hub
    from awm.social.dao import SocialDAO, init

    init()
    pusher = _FakePush()
    monkeypatch.setattr(hub, "_dao", SocialDAO())
    monkeypatch.setattr(hub, "_connectors", {"bot": _FakeConn(
        [("D1", "tony", "dm"), ("C1", "lab", "channel")])})
    monkeypatch.setattr(hub, "_seen", set())
    monkeypatch.setattr(hub, "_adapter", None)
    monkeypatch.setattr(hub, "_pusher", pusher)
    hub.pushed = pusher.lines
    return hub


def _as(claim, node="altair"):
    return {"_caller_session": claim}, f"peer:{node}"


async def test_mount_holds_the_seat_and_lists_it(wired):
    extra, as_ = _as(ME)
    r = await wired._h_mount({"account": "bot", "channel": "#lab", **extra}, as_)
    assert r["ok"] and r["seat"]["id"] == "C1"
    assert r["seat"]["holder"]["job"] == ME["job"]
    assert "channel=C1" in r["reply"] and "thread=" in r["reply"]
    seats = {s["id"]: s for s in (await wired._h_seats({"account": "bot"}))["seats"]}
    assert seats["C1"]["holder"]["node"] == "altair"
    assert seats["D1"]["holder"] is None


async def test_a_held_seat_refuses_another_session_until_forced(wired):
    mine, as_ = _as(ME)
    theirs, as2 = _as(OTHER, node="capella")
    await wired._h_mount({"account": "bot", "channel": "C1", **mine}, as_)
    r = await wired._h_mount({"account": "bot", "channel": "C1", **theirs}, as2)
    assert not r["ok"] and "aaaa1111@altair (opal desk)" in r["error"]
    assert wired.pushed == []
    r = await wired._h_mount({"account": "bot", "channel": "C1", "force": True, **theirs}, as2)
    assert r["ok"] and r["evicted"]["job"] == ME["job"]
    node, job, line = wired.pushed[0]
    assert (node, job) == ("altair", ME["job"])
    assert line.startswith("slack seat lost: lab") and "bbbb2222@capella" in line


async def test_remounting_your_own_seat_is_not_an_eviction(wired):
    mine, as_ = _as(ME)
    await wired._h_mount({"account": "bot", "channel": "C1", **mine}, as_)
    r = await wired._h_mount({"account": "bot", "channel": "C1", **mine}, as_)
    assert r["ok"] and r["evicted"] is None and wired.pushed == []


async def test_a_discord_dm_is_reserved(wired, monkeypatch):
    conn = _FakeConn([("42", "Guild#general", "text")], dms=[("99", "tony")])
    conn.platform = "discord"
    monkeypatch.setitem(wired._connectors, "bot", conn)
    mine, as_ = _as(ME)
    for ref in ("99", "dm:188743359983124480"):
        r = await wired._h_mount({"account": "bot", "channel": ref, **mine}, as_)
        assert not r["ok"] and "reserved for notifications" in r["error"]
    r = await wired._h_mount({"account": "bot", "channel": "nowhere", **mine}, as_)
    assert not r["ok"] and "not in 'nowhere'" in r["error"]


async def test_mount_needs_a_background_session(wired):
    r = await wired._h_mount({"account": "bot", "channel": "C1"}, None)
    assert not r["ok"] and "no calling session" in r["error"]
    extra, as_ = _as({**ME, "kind": "interactive", "job": ""})
    r = await wired._h_mount({"account": "bot", "channel": "C1", **extra}, as_)
    assert not r["ok"] and "background session" in r["error"]
    # A claim without the edge's peer stamp names no node and is not trusted.
    r = await wired._h_mount({"account": "bot", "channel": "C1", "_caller_session": ME}, None)
    assert not r["ok"]


async def test_a_local_caller_is_read_from_its_pid(wired, monkeypatch):
    from awm import config
    from awm.social import seats
    monkeypatch.setattr(seats, "session_claim", lambda pid: ME if pid == 4242 else None)
    monkeypatch.setattr(config, "node_name", lambda: "mira")
    r = await wired._h_mount({"account": "bot", "channel": "C1", "_caller_pid": 4242,
                              "_caller_session": OTHER}, "peer:capella")
    assert r["seat"]["holder"]["node"] == "mira" and r["seat"]["holder"]["job"] == ME["job"]


async def test_unmount_frees_only_your_own_seats(wired):
    mine, as_ = _as(ME)
    theirs, as2 = _as(OTHER)
    await wired._h_mount({"account": "bot", "channel": "C1", **mine}, as_)
    await wired._h_mount({"account": "bot", "channel": "D1", **theirs}, as2)
    r = await wired._h_unmount({"account": "bot", "channel": "tony", **mine}, as_)
    assert not r["ok"] and "not by you" in r["error"]
    r = await wired._h_unmount({"account": "bot", **mine}, as_)
    assert r["released"] == [{"id": "C1", "name": "lab"}]
    assert wired._dao.holder("bot", "C1") is None
    assert wired._dao.holder("bot", "D1")["job"] == OTHER["job"]


async def test_a_message_in_a_held_seat_is_pushed_to_the_holder(wired):
    mine, as_ = _as(ME)
    await wired._h_mount({"account": "bot", "channel": "D1", **mine}, as_)
    await wired._on_message(_msg(text="first\nsecond"))
    await wired._on_message(_msg(channel_id="C1", message_id="11.1", text="unheld"))
    assert wired.pushed == [("altair", ME["job"], "slack from tony: first ⏎ second")]


def test_push_line_marks_thread_replies_and_cuts_long_text():
    from awm.social import seats
    from awm.social.connectors.base import Attachment
    reply = _msg(thread_id="9.5", message_id="10.1", text="yes")
    assert seats.push_line(reply) == "slack from tony ↳9.5: yes"
    parent = _msg(thread_id="10.1", message_id="10.1", platform="discord")
    assert seats.push_line(parent) == "discord from tony: hi"
    long = seats.push_line(_msg(text="x" * (seats.TEXT_MAX + 50)))
    assert long.endswith("…(cut, full text in social history)")
    assert len(long) < seats.TEXT_MAX + 80
    files = _msg(text="", attachments=[Attachment(idx=0, filename="a"),
                                       Attachment(idx=1, filename="b")])
    assert seats.push_line(files) == "slack from tony: [+2 files]"


async def test_pusher_delivers_in_order_and_retries_a_refusal():
    from awm.social import seats
    sent, outcomes = [], iter(["retry", "ok", "ok", "ok"])
    slept = []

    async def send(node, job, line):
        sent.append(line)
        return next(outcomes)

    async def sleep(s):
        slept.append(s)

    p = seats.Pusher(send=send, sleep=sleep)
    for line in ("a", "b", "c"):
        p.push("altair", "j1", line)
    await p.idle()
    assert sent == ["a", "a", "b", "c"] and slept == [2.0]


async def test_pusher_drops_after_the_retry_window_and_releases_a_gone_holder():
    from awm.social import seats
    gone = []

    async def refuse(node, job, line):
        return "retry"

    p = seats.Pusher(send=refuse, retry_s=0.05, sleep=lambda s: asyncio.sleep(0))
    p.push("altair", "j1", "a")
    await p.idle()

    sent = []

    async def vanish(node, job, line):
        sent.append(line)
        return "gone"

    p = seats.Pusher(send=vanish, on_gone=lambda n, j: gone.append((n, j)))
    p.push("altair", "j1", "a")
    p.push("altair", "j1", "b")
    await p.idle()
    assert sent == ["a"] and gone == [("altair", "j1")]


async def test_prune_releases_dead_holders_and_keeps_unreachable_nodes(wired):
    from awm.social import seats
    for job, channel, node in (("j1", "C1", "altair"), ("j2", "D1", "capella")):
        wired._dao.mount("bot", channel, "", {"node": node, "job": job})

    async def live(node):
        return set() if node == "altair" else None

    freed = await seats.prune(wired._dao, list_jobs=live)
    assert [r["channel_id"] for r in freed] == ["C1"]
    assert wired._dao.holder("bot", "D1")["job"] == "j2"


async def test_mounts_survive_a_restart(wired):
    from awm.social.dao import SocialDAO
    wired._dao.mount("bot", "C1", "lab", {"node": "altair", "job": "j1", "name": "n"})
    assert SocialDAO().holder("bot", "C1")["session_name"] == "n"


async def test_a_borrower_forwards_its_session_not_its_pid(wired, monkeypatch):
    from awm import gatewayclient as gc
    from awm.social import seats
    calls = []

    async def call_peer(peer, service, fn, args=None, **kw):
        calls.append((peer, fn, args))
        return {"ok": True}

    monkeypatch.setattr(gc, "peer_env", lambda var: "mira")
    monkeypatch.setattr(gc, "call_peer", call_peer)
    monkeypatch.setattr(wired, "_connectors", {})
    monkeypatch.setattr(seats, "session_claim", lambda pid: ME)
    await wired.HANDLERS["mount"]({"account": "slack-bot", "channel": "D1",
                                   "_caller_pid": 7, "_caller_session": OTHER}, None)
    assert calls == [("mira", "mount", {"account": "slack-bot", "channel": "D1",
                                        "_caller_session": ME})]
