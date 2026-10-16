"""``social react``: Discord reactions, the Slack emoji-message stop gap, and
the refusal on platforms without reactions."""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.smoke]


async def _noop(_m):
    return None


def _build(**kw):
    from awm.social.config import AccountConfig
    from awm.social.connectors import build
    return build(AccountConfig(**kw), _noop)


# -- emoji names --------------------------------------------------------------

def test_emoji_accepts_a_character_a_name_or_colons():
    from awm.social.emoji import to_char, to_name
    assert to_char("👀") == to_char("eyes") == to_char(":eyes:") == "👀"
    assert to_name("👀") == to_name(":eyes:") == "eyes"
    assert to_name("❤️") == to_name("❤") == "heart"
    assert to_char("partyparrot") is None
    assert to_name(":partyparrot:") == "partyparrot"


# -- discord ------------------------------------------------------------------

class _Message:
    def __init__(self, channel, error=None):
        self.id = 42
        self.channel = channel
        self.added, self.removed = [], []
        self.error = error

    async def add_reaction(self, emoji):
        if self.error:
            raise self.error
        self.added.append(emoji)

    async def remove_reaction(self, emoji, member):
        self.removed.append((emoji, member))


class _Channel:
    id = 7

    def __init__(self, error=None):
        self.message = _Message(self, error)

    async def fetch_message(self, mid):
        assert mid == 42
        return self.message


class _Client:
    user = "opal-bot"

    def __init__(self, channel):
        self.channel = channel

    def get_channel(self, cid):
        assert cid == 7
        return self.channel


def _discord(error=None):
    async def _ready():
        return None

    conn = _build(name="bot", platform="discord", token="t")
    channel = _Channel(error)
    conn._client = _Client(channel)
    conn._wait_ready = _ready
    return conn, channel.message


async def test_discord_adds_and_removes_a_reaction():
    conn, message = _discord()
    r = await conn.react("7", "42", ":eyes:")
    assert message.added == ["👀"]
    assert r == {"as": "reaction", "emoji": "👀", "removed": False,
                 "message_id": "42", "channel_id": "7"}
    await conn.react("7", "42", "👀", remove=True)
    assert message.removed == [("👀", "opal-bot")]


async def test_discord_custom_emoji_passes_through():
    conn, message = _discord()
    await conn.react("7", "42", "<:opal:123>")
    assert message.added == ["<:opal:123>"]


async def test_discord_forbidden_is_a_refusal():
    import discord

    class _Resp:
        status = 403
        reason = "Forbidden"

    conn, _ = _discord(discord.Forbidden(_Resp(), "Missing Permissions"))
    with pytest.raises(RuntimeError, match="may not react"):
        await conn.react("7", "42", "👀")


# -- slack stop gap -----------------------------------------------------------

def _slack_connectors():
    return [
        _build(name="opal", platform="slack", token="xoxb-1", app_token="xapp-1"),
        _build(name="tony", platform="slack", token="xoxp-1", app_token="xapp-1"),
        _build(name="mira", platform="slack", token="", source="mira",
               mira_url="https://h:7822", mira_token="tok"),
    ]


@pytest.mark.parametrize("conn", _slack_connectors(), ids=["bot", "user", "mira"])
async def test_slack_posts_the_emoji_as_a_message(conn):
    sent = []

    async def send(channel, text, *, thread=None):
        sent.append((channel, text, thread))
        return {"message_id": "9.9", "channel_id": channel, "ts": "9.9"}

    conn.send = send
    r = await conn.react("D1", "1.1", "eyes", thread="1.0")
    assert sent == [("D1", "👀", "1.0")]
    assert r["as"] == "message" and r["message_id"] == "9.9"
    with pytest.raises(RuntimeError, match="no reaction to remove"):
        await conn.react("D1", "1.1", "eyes", remove=True)
    with pytest.raises(RuntimeError, match="no known character"):
        await conn.react("D1", "1.1", "partyparrot")
    assert len(sent) == 1


async def test_gmail_and_teams_refuse():
    gmail = _build(name="g", platform="gmail", token="pw", address="a@b.c")
    teams = _build(name="t", platform="teams", token="", source="mira",
                   mira_url="https://h:7822", mira_token="tok")
    for conn in (gmail, teams):
        with pytest.raises(NotImplementedError, match="does not support reactions"):
            await conn.react("C1", "1", "👀")


# -- the verb -----------------------------------------------------------------

async def test_react_verb_passes_through_to_the_connector(monkeypatch):
    from awm.social import hub_adapter as hub

    calls = []

    class _Conn:
        async def react(self, channel, message_id, emoji, *, remove, thread):
            calls.append((channel, message_id, emoji, remove, thread))
            return {"as": "reaction", "emoji": emoji}

    monkeypatch.setitem(hub._connectors, "bot", _Conn())
    r = await hub.HANDLERS["react"]({"account": "bot", "channel": "C1",
                                     "message_id": 5, "emoji": "👀"})
    assert r == {"ok": True, "as": "reaction", "emoji": "👀"}
    assert calls == [("C1", "5", "👀", False, None)]
    fn = next(f for f in hub.API_MANIFEST["functions"] if f["name"] == "react")
    assert fn["effect"] == "write"
    assert {p["name"] for p in fn["params"] if p["required"]} == {
        "account", "channel", "message_id", "emoji"}
