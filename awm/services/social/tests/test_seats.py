"""Seats: mount, unmount and seats verbs, the pushed line, delivery and prune."""

from __future__ import annotations

import asyncio

import pytest

pytestmark = [pytest.mark.unit]

ME = {"kind": "bg", "job": "aaaa1111", "name": "opal desk"}
#: What reflection's session_of answers for ME.
RECORD = {"hosting": "background", "job": "aaaa1111", "name": "opal desk",
          "session_id": "cc" + "a" * 32}
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
    from awm.gatewayclient import sessions
    monkeypatch.setattr(sessions, "session_of", lambda pid: RECORD if pid == 4242 else None)
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
    assert wired.pushed == [("altair", ME["job"], "slack [untrusted] from tony [channel=D1 message_id=10.1]: first ⏎ second")]


async def test_a_message_kept_from_the_holder_is_not_pushed(wired):
    mine, as_ = _as(ME)
    await wired._h_mount({"account": "bot", "channel": "D1", **mine}, as_)
    await wired._on_message(_msg(text="/new-agen x", to_holder=False))
    assert wired.pushed == []


async def test_replies_under_a_quiet_send_are_kept_from_the_holder(wired, monkeypatch):
    from awm.social.dao import SocialDAO

    class _Sender:
        platform = "slack"
        sends_files = False

        async def send(self, channel, text, *, thread=None):
            return {"message_id": "20.1", "channel_id": "D1", "ts": "20.1"}

    monkeypatch.setitem(wired._connectors, "bot", _Sender())
    mine, as_ = _as(ME)
    wired._dao.mount("bot", "D1", "tony", {"node": "altair", "job": ME["job"]})
    await wired._h_send({"account": "bot", "channel": "D1", "text": "approve?", "quiet": True})
    monkeypatch.setattr(wired, "_dao", SocialDAO())
    await wired._on_message(_msg(thread_id="20.1", message_id="20.2", text="approve"))
    await wired._on_message(_msg(thread_id="20.1", message_id="20.1", text="approve?"))
    await wired._on_message(_msg(thread_id="9.5", message_id="20.3", text="other thread"))
    assert [line for _, _, line in wired.pushed] == [
        "slack [untrusted] from tony ↳9.5 [channel=D1 message_id=20.3]: other thread"]


def test_push_line_marks_thread_replies_and_cuts_long_text():
    from awm.social import seats
    from awm.social.connectors.base import Attachment
    reply = _msg(thread_id="9.5", message_id="10.1", text="yes")
    assert seats.push_line(reply) == "slack [untrusted] from tony ↳9.5 [channel=D1 message_id=10.1]: yes"
    parent = _msg(thread_id="10.1", message_id="10.1", platform="discord")
    assert seats.push_line(parent) == "discord [untrusted] from tony [channel=D1 message_id=10.1]: hi"
    long = seats.push_line(_msg(text="x" * (seats.TEXT_MAX + 50)))
    assert long.endswith("…(cut, full text in social history)")
    assert len(long) < seats.TEXT_MAX + 120
    files = _msg(text="", attachments=[Attachment(idx=0, filename="a"),
                                       Attachment(idx=1, filename="b")])
    assert seats.push_line(files) == "slack [untrusted] from tony [channel=D1 message_id=10.1]: [+2 files]"
    assert seats.push_line(_msg(message_id="")) == "slack [untrusted] from tony: hi"


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
    from awm.gatewayclient import holders
    calls = []

    async def call_peer(peer, service, fn, args=None, **kw):
        calls.append((peer, fn, args))
        return {"ok": True}

    monkeypatch.setattr(gc, "peer_env", lambda var: "mira")
    monkeypatch.setattr(gc, "call", lambda tool, verb, args=None, *, peer=None, **kw: (call_peer)(peer, tool, verb, args, **kw))
    monkeypatch.setattr(wired, "_connectors", {})
    monkeypatch.setattr(holders, "claim", lambda pid: ME)
    await wired.HANDLERS["mount"]({"account": "slack-bot", "channel": "D1",
                                   "_caller_pid": 7, "_caller_session": OTHER}, None)
    assert calls == [("mira", "mount", {"account": "slack-bot", "channel": "D1",
                                        "_caller_session": ME})]


# -- trust (channels.md R70, users.md R126 item 2) ------------------------------

def _acct(**kw):
    from awm.social.config import AccountConfig
    return AccountConfig(name="slack-bot", platform="slack", token="xoxb-1", **kw)


def test_seat_trust_levels():
    from awm.social import seats
    cfg = _acct(trusted_chats=("DTONY",))
    assert seats.seat_trust(cfg, "DTONY") == "trusted"
    assert seats.seat_trust(cfg, "DBETH") == "cooperative"
    assert seats.seat_trust(None, "D1") == "untrusted"


def test_push_line_carries_the_level():
    from awm.social import seats
    from awm.social.connectors.base import InboundMessage
    m = InboundMessage(account="slack-bot", platform="slack", channel_id="D1",
                       channel_name="", thread_id="", sender_id="U1",
                       sender_name="beth", message_id="1.0", ts="1.0", text="hi")
    assert seats.push_line(m, "cooperative").startswith("slack [cooperative] from beth")


def test_a_lab_seat_reads_only_its_own_chats(awm_workspace):
    from awm.social import dao, seats
    dao.init()
    store = dao.SocialDAO()
    cfg = _acct(trusted_chats=("DTONY",))
    beth = {"node": "altair", "job": "j-beth", "name": "seat:slack/DM/beth"}
    main = {"node": "altair", "job": "j-main", "name": "seat:slack/DM/tony"}
    store.mount("slack-bot", "DBETH", "beth", beth)
    store.mount("slack-bot", "DTONY", "tony", main)
    assert seats.readable_chats(store, cfg, beth) == {"DBETH"}
    assert seats.readable_chats(store, cfg, main) is None
    assert seats.readable_chats(store, cfg, {"node": "mizar", "job": "x"}) is None
