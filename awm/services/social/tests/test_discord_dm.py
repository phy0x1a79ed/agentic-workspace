"""The Discord connector reaches a user's DM through a ``dm:<user_id>`` channel."""

from __future__ import annotations

import asyncio

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.smoke]


class _Sent:
    def __init__(self, channel):
        self.id = 1
        self.channel = channel
        self.created_at = None


class _DM:
    id = 555
    recipient = "tester#0001"

    def __init__(self):
        self.sent = []
        self.suppressed = []
        self.files = []

    async def send(self, text, suppress_embeds=False, files=()):
        self.sent.append(text)
        self.suppressed.append(suppress_embeds)
        self.files.append([f.filename for f in files])
        return _Sent(self)


class _User:
    def __init__(self):
        self.dm_channel = None
        self.opened = 0

    async def create_dm(self):
        self.opened += 1
        self.dm_channel = _DM()
        return self.dm_channel


class _Client:
    guilds = ()

    def __init__(self):
        self.users = {}
        self.fetched = []

    def get_user(self, uid):
        return self.users.get(uid)

    async def fetch_user(self, uid):
        self.fetched.append(uid)
        self.users[uid] = _User()
        return self.users[uid]

    def get_channel(self, cid):
        return None

    async def fetch_channel(self, cid):
        raise AssertionError("a dm: target must not resolve as a channel id")


def _connector():
    from awm.social.config import AccountConfig
    from awm.social.connectors import build

    async def _noop(_m):
        return None

    async def _ready():
        return None

    conn = build(AccountConfig(name="bot", platform="discord", token="t"), _noop)
    conn._client = _Client()
    conn._wait_ready = _ready
    return conn


def test_send_to_dm_opens_it_once():
    conn = _connector()

    async def run():
        first = await conn.send("dm:4242", "one")
        await conn.send("dm:4242", "two")
        return first

    first = asyncio.run(run())
    user = conn._client.users[4242]
    assert first["channel_id"] == "555"
    assert user.opened == 1
    assert conn._client.fetched == [4242]
    assert user.dm_channel.sent == ["one", "two"]


def test_a_dm_never_unfurls_its_links():
    conn = _connector()
    asyncio.run(conn.send("dm:4242", "see https://example.com"))
    assert conn._client.users[4242].dm_channel.suppressed == [True]


def test_send_attaches_text_files():
    conn = _connector()
    asyncio.run(conn.send("dm:4242", "here", files=[{"filename": "prompt.txt", "text": "x"}]))
    asyncio.run(conn.send("dm:4242", "bare"))
    assert conn._client.users[4242].dm_channel.files == [["prompt.txt"], []]


def test_open_dm_accepts_bare_id_and_dm_form():
    conn = _connector()
    ch = asyncio.run(conn.open_dm("4242"))
    assert (ch.id, ch.kind, ch.name) == ("555", "dm", "tester#0001")
    assert asyncio.run(conn.open_dm("dm:4242")).id == "555"


def test_an_operators_dm_is_a_seat(awm_workspace):
    from awm.social import operators

    operators.add_operator("discord", "4242", "tony")
    operators.add_operator("slack", "U1", "tony")
    seats = asyncio.run(_connector().list_seats())
    assert [(c.id, c.kind) for c in seats] == [("555", "dm")]


def test_a_dm_without_an_operator_is_no_seat(awm_workspace):
    assert asyncio.run(_connector().list_seats()) == []


@pytest.mark.parametrize(("bot", "kind", "text", "heard"), [
    (False, "default", "tell the fig session yes", True),
    (False, "reply", "yes", True),
    (True, "default", "Duo approvals armed", False),
    (False, "chat_input_command", "", False),
    (False, "default", "/new-agen fix the build", False),
])
def test_a_dm_seat_hears_only_plain_words(bot, kind, text, heard):
    import discord
    from types import SimpleNamespace

    from awm.social.connectors.discord_conn import _natural

    dm = discord.DMChannel.__new__(discord.DMChannel)
    msg = SimpleNamespace(channel=dm, author=SimpleNamespace(bot=bot),
                          type=getattr(discord.MessageType, kind), content=text)
    assert _natural(msg) is heard


def test_a_guild_message_always_reaches_the_holder():
    from types import SimpleNamespace

    from awm.social.connectors.discord_conn import _natural

    assert _natural(SimpleNamespace(channel=object(), author=SimpleNamespace(bot=True),
                                    type=None, content="/x"))
