"""Tests for the Discord ``/approve`` slash command and its adapter emit.

The command surface is exercised offline: we build the connector's command tree
(no live bot), fetch the registered ``approve`` command, and invoke its callback
with a fake interaction. The adapter-side emit is tested by monkeypatching the
module ``_adapter`` with a capturing fake.
"""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.smoke]


# -- fakes ------------------------------------------------------------------

class _FakeResponse:
    def __init__(self):
        self.messages = []  # list of (content, ephemeral)

    async def send_message(self, content, ephemeral=False):
        self.messages.append((content, ephemeral))


class _FakeUser:
    id = 4242

    def __str__(self):
        return "tester#0001"


class _FakeInteraction:
    def __init__(self, guild):
        self.guild = guild
        self.user = _FakeUser()
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
            channel_id="9", is_dm=True))

        assert len(emitted) == 1
        topic, payload = emitted[0]
        assert topic == "command"
        assert payload["command"] == "approve"
        assert payload["device"] == "cwl"
        assert payload["is_dm"] is True

    @pytest.mark.asyncio
    async def test_no_adapter_is_noop(self, monkeypatch):
        from awm.social import hub_adapter
        from awm.social.connectors import Command

        monkeypatch.setattr(hub_adapter, "_adapter", None)
        # Must not raise when no adapter is attached.
        await hub_adapter._on_command(Command(
            account="bot", platform="discord", name="approve"))
