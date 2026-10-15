"""Tests for the Discord slash commands (``/approve``, ``/reject``,
``/new-agent``), the operator gate, and the adapter emit.

The command surface is exercised offline: we build the connector's command tree
(no live bot), fetch the registered ``approve`` command, and invoke its callback
with a fake interaction. The adapter-side emit is tested by monkeypatching the
module ``_adapter`` with a capturing fake.
"""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.smoke]


@pytest.fixture(autouse=True)
def _operator(awm_workspace):
    """Sender 4242 is a whitelisted operator; anyone else is not."""
    from awm.social import operators
    operators.add_operator("discord", "4242", "tony")


# -- fakes ------------------------------------------------------------------

class _FakeResponse:
    def __init__(self):
        self.messages = []  # list of (content, ephemeral)

    async def send_message(self, content, ephemeral=False):
        self.messages.append((content, ephemeral))


class _FakeUser:
    def __init__(self, uid=4242):
        self.id = uid

    def __str__(self):
        return "tester#0001"


class _FakeInteraction:
    def __init__(self, guild, uid=4242):
        self.guild = guild
        self.user = _FakeUser(uid)
        self.channel_id = 999
        self.response = _FakeResponse()


def _connector(on_command):
    from awm.social.connectors import build
    from awm.social.config import AccountConfig

    async def _noop(_m):
        return None

    conn = build(
        AccountConfig(name="bot", platform="discord", token="t"),
        _noop, on_command)
    conn._build_client()  # registers the command tree (no network)
    return conn


def _approve_cmd(conn):
    return conn._tree.get_command("approve")


# -- where the commands show ------------------------------------------------

def test_every_command_is_registered_for_the_bot_dm_only():
    """Discord hides each command in servers and group DMs: the synced payload
    allows only the bot DM context (1) on a guild install (0)."""
    conn = _connector(None)
    payloads = [c.to_dict(conn._tree) for c in conn._tree.get_commands()]
    assert {p["name"] for p in payloads} == {"approve", "reject", "new-agent"}
    for p in payloads:
        assert p["contexts"] == [1] and p["integration_types"] == [0]


# -- on_command wiring ------------------------------------------------------

class TestBuildForwardsOnCommand:
    def test_on_command_stored_on_connector(self):
        async def _cb(_c):
            return None
        conn = _connector(_cb)
        assert conn.on_command is _cb

    def test_other_platforms_accept_and_ignore_on_command(self):
        from awm.social.connectors import build
        from awm.social.config import AccountConfig

        async def _noop(_m):
            return None

        async def _cb(_c):
            return None

        g = build(AccountConfig(name="g", platform="gmail", token="pw",
                                address="me@x.com"), _noop, _cb)
        assert g.on_command is _cb  # stored, simply never invoked


# -- the /approve callback --------------------------------------------------

class TestApproveCommand:
    @pytest.mark.asyncio
    async def test_dm_default_device_fires_command(self):
        captured = []

        async def cap(cmd):
            captured.append(cmd)

        conn = _connector(cap)
        cmd = _approve_cmd(conn)
        inter = _FakeInteraction(guild=None)
        await cmd.callback(inter)

        assert len(captured) == 1
        c = captured[0]
        assert c.name == "approve"
        assert c.options == {"device": "cwl"}
        assert c.is_dm is True
        assert c.user_id == "4242"
        assert c.channel_id == "999"
        # An ack was sent (non-ephemeral, visible in the DM).
        assert inter.response.messages
        assert inter.response.messages[0][1] is False

    @pytest.mark.asyncio
    async def test_dm_explicit_device(self):
        captured = []

        async def cap(cmd):
            captured.append(cmd)

        conn = _connector(cap)
        cmd = _approve_cmd(conn)
        inter = _FakeInteraction(guild=None)
        await cmd.callback(inter, device="alliance")

        assert captured[0].options == {"device": "alliance"}

    @pytest.mark.asyncio
    async def test_guild_invocation_rejected(self):
        captured = []

        async def cap(cmd):
            captured.append(cmd)

        conn = _connector(cap)
        cmd = _approve_cmd(conn)
        inter = _FakeInteraction(guild=object())  # not a DM
        await cmd.callback(inter)

        assert captured == []  # on_command never fired
        # Reject reply is ephemeral.
        assert inter.response.messages
        assert inter.response.messages[0][1] is True


# -- operator gate ----------------------------------------------------------

class TestOperatorGate:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("name,kwargs", [
        ("approve", {}),
        ("reject", {"ref": "r1"}),
        ("new-agent", {}),
    ])
    async def test_non_operator_dropped_and_refused(self, name, kwargs):
        captured = []

        async def cap(cmd):
            captured.append(cmd)

        conn = _connector(cap)
        inter = _FakeInteraction(guild=None, uid=777)
        await conn._tree.get_command(name).callback(inter, **kwargs)

        assert captured == []
        assert len(inter.response.messages) == 1
        text, ephemeral = inter.response.messages[0]
        assert ephemeral is True
        assert "not an operator" in text

    @pytest.mark.asyncio
    async def test_operator_command_carries_awm_user(self):
        captured = []

        async def cap(cmd):
            captured.append(cmd)

        conn = _connector(cap)
        await _approve_cmd(conn).callback(_FakeInteraction(guild=None))
        assert captured[0].operator == "tony"


# -- /reject and /new-agent -------------------------------------------------

class TestRejectCommand:
    @pytest.mark.asyncio
    async def test_fires_with_ref_and_reason_and_acks_ephemerally(self):
        captured = []

        async def cap(cmd):
            captured.append(cmd)

        conn = _connector(cap)
        inter = _FakeInteraction(guild=None)
        await conn._tree.get_command("reject").callback(
            inter, ref="req-7", reason="too broad")

        assert captured[0].name == "reject"
        assert captured[0].options == {"ref": "req-7", "reason": "too broad"}
        content, ephemeral = inter.response.messages[0]
        assert ephemeral is True and "req-7" in content

    @pytest.mark.asyncio
    async def test_reason_is_optional(self):
        captured = []

        async def cap(cmd):
            captured.append(cmd)

        conn = _connector(cap)
        await conn._tree.get_command("reject").callback(
            _FakeInteraction(guild=None), ref="req-7")
        assert captured[0].options == {"ref": "req-7", "reason": ""}

    @pytest.mark.asyncio
    async def test_guild_invocation_rejected(self):
        captured = []

        async def cap(cmd):
            captured.append(cmd)

        conn = _connector(cap)
        inter = _FakeInteraction(guild=object())
        await conn._tree.get_command("reject").callback(inter, ref="r")
        assert captured == []
        assert inter.response.messages[0][1] is True


class TestNewAgentCommand:
    @pytest.mark.asyncio
    async def test_fires_with_options_and_dm_reply_target(self):
        captured = []

        async def cap(cmd):
            captured.append(cmd)

        conn = _connector(cap)
        inter = _FakeInteraction(guild=None)
        await conn._tree.get_command("new-agent").callback(inter, prompt="fix the tests")

        assert captured[0].name == "new_agent"
        assert captured[0].options == {"prompt": "fix the tests", "reply_to": "dm:4242"}
        content, ephemeral = inter.response.messages[0]
        assert ephemeral is True and "agent" in content

    @pytest.mark.asyncio
    async def test_the_prompt_is_optional(self):
        captured = []

        async def cap(cmd):
            captured.append(cmd)

        conn = _connector(cap)
        await conn._tree.get_command("new-agent").callback(
            _FakeInteraction(guild=None))
        assert captured[0].options == {"prompt": "", "reply_to": "dm:4242"}


# -- adapter emit -----------------------------------------------------------

class TestOnCommandEmit:
    @pytest.mark.asyncio
    async def test_emits_command_payload(self, monkeypatch):
        from awm.social import hub_adapter
        from awm.social.connectors import Command

        emitted = []

        class _FakeAdapter:
            async def emit(self, topic, payload):
                emitted.append((topic, payload))

        monkeypatch.setattr(hub_adapter, "_adapter", _FakeAdapter())
        await hub_adapter._on_command(Command(
            account="bot", platform="discord", name="approve",
            options={"device": "cwl"}, user_id="1", user_name="u",
            channel_id="9", is_dm=True, operator="tony"))

        assert len(emitted) == 1
        topic, payload = emitted[0]
        assert topic == "command"
        assert payload["command"] == "approve"
        assert payload["device"] == "cwl"
        assert payload["is_dm"] is True
        assert payload["operator"] == "tony"

    @pytest.mark.asyncio
    async def test_reject_and_new_agent_payloads(self, monkeypatch):
        from awm.social import hub_adapter
        from awm.social.connectors import Command

        emitted = []

        class _FakeAdapter:
            async def emit(self, topic, payload):
                emitted.append((topic, payload))

        monkeypatch.setattr(hub_adapter, "_adapter", _FakeAdapter())
        await hub_adapter._on_command(Command(
            account="bot", platform="discord", name="reject",
            options={"ref": "r1", "reason": "no"}, user_id="1",
            is_dm=True, operator="tony"))
        await hub_adapter._on_command(Command(
            account="bot", platform="discord", name="new_agent",
            options={"prompt": "p", "reply_to": "dm:1"},
            user_id="1", is_dm=True, operator="tony"))

        reject, new_agent = (p for _, p in emitted)
        assert (reject["command"], reject["ref"], reject["reason"],
                reject["operator"]) == ("reject", "r1", "no", "tony")
        assert (new_agent["command"], new_agent["prompt"], new_agent["reply_to"],
                new_agent["operator"]) == ("new_agent", "p", "dm:1", "tony")
        assert new_agent["account"] == "bot"

    @pytest.mark.asyncio
    async def test_no_adapter_is_noop(self, monkeypatch):
        from awm.social import hub_adapter
        from awm.social.connectors import Command

        monkeypatch.setattr(hub_adapter, "_adapter", None)
        # Must not raise when no adapter is attached.
        await hub_adapter._on_command(Command(
            account="bot", platform="discord", name="approve"))
