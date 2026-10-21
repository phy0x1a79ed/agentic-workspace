"""``social react``: Discord and Slack reactions, and the refusal on platforms
without reactions."""

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


# -- slack --------------------------------------------------------------------

def _slack_bot(monkeypatch, error=None):
    from awm.social.connectors.slack_bot_conn import SlackApiError
    conn = _build(name="opal", platform="slack", token="xoxb-1", app_token="xapp-1")
    calls = []

    async def call(method, **params):
        calls.append((method, params))
        if error:
            raise SlackApiError(method, error)
        return {}

    monkeypatch.setattr(conn, "_call", call)
    return conn, calls


async def test_slack_bot_adds_and_removes_by_name(monkeypatch):
    conn, calls = _slack_bot(monkeypatch)
    r = await conn.react("D1", "1.1", "👀")
    assert r == {"as": "reaction", "emoji": "eyes", "removed": False,
                 "message_id": "1.1", "channel_id": "D1"}
    await conn.react("D1", "1.1", ":eyes:", remove=True)
    assert calls == [
        ("reactions.add", {"channel": "D1", "timestamp": "1.1", "name": "eyes"}),
        ("reactions.remove", {"channel": "D1", "timestamp": "1.1", "name": "eyes"})]


@pytest.mark.parametrize("error", ["already_reacted", "no_reaction"])
async def test_slack_reaching_the_asked_state_is_ok(monkeypatch, error):
    conn, _ = _slack_bot(monkeypatch, error)
    r = await conn.react("D1", "1.1", "eyes", remove=error == "no_reaction")
    assert r["as"] == "reaction"


async def test_slack_without_reactions_write_refuses(monkeypatch):
    conn, _ = _slack_bot(monkeypatch, "missing_scope")
    with pytest.raises(RuntimeError, match="reactions:write"):
        await conn.react("D1", "1.1", "eyes")
    conn, _ = _slack_bot(monkeypatch, "message_not_found")
    with pytest.raises(RuntimeError, match="message_not_found"):
        await conn.react("D1", "1.1", "eyes")


async def test_slack_user_reacts_through_the_sdk(monkeypatch):
    from slack_sdk.errors import SlackApiError
    conn = _build(name="tony", platform="slack", token="xoxp-1", app_token="xapp-1")
    calls = []

    class _Web:
        async def reactions_add(self, **kw):
            calls.append(("add", kw))
            raise SlackApiError("dup", {"ok": False, "error": "already_reacted"})

        async def reactions_remove(self, **kw):
            calls.append(("remove", kw))
            return {"ok": True}

    monkeypatch.setattr(conn, "_web_client", lambda: _Web())
    assert (await conn.react("D1", "1.1", "👀"))["emoji"] == "eyes"
    assert (await conn.react("D1", "1.1", "👀", remove=True))["removed"]
    assert calls == [("add", {"channel": "D1", "timestamp": "1.1", "name": "eyes"}),
                     ("remove", {"channel": "D1", "timestamp": "1.1", "name": "eyes"})]


async def test_mira_posts_the_reaction_to_the_daemon(monkeypatch):
    conn = _build(name="mira", platform="slack", token="", source="mira",
                  mira_url="https://h:7822", mira_token="tok")
    posts = []

    async def post(path, body, timeout=30):
        posts.append((path, body))
        return {"ok": True}

    monkeypatch.setattr(conn, "_post", post)
    r = await conn.react("D1", "1.1", ":eyes:", remove=True)
    assert posts == [("/v1/slack/react", {"channel": "D1", "message_id": "1.1",
                                          "emoji": "eyes", "remove": True})]
    assert r["as"] == "reaction" and r["removed"]


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
        async def react(self, channel, message_id, emoji, *, remove):
            calls.append((channel, message_id, emoji, remove))
            return {"as": "reaction", "emoji": emoji}

    monkeypatch.setitem(hub._connectors, "bot", _Conn())
    r = await hub.HANDLERS["react"]({"account": "bot", "channel": "C1",
                                     "message_id": 5, "emoji": "👀"})
    assert r == {"ok": True, "as": "reaction", "emoji": "👀"}
    assert calls == [("C1", "5", "👀", False)]
    fn = next(f for f in hub.API_MANIFEST["functions"] if f["name"] == "react")
    assert fn["effect"] == "write"
    assert {p["name"] for p in fn["params"] if p["required"]} == {
        "account", "channel", "message_id", "emoji"}
