"""Tests for the social_history handler: fetch → persist (dedupe) → return.

Drives ``_h_history`` with a fake connector so no live platform is needed; the
key behavioural guarantee is idempotency — re-fetching the same history adds no
duplicate rows, and the persisted messages become visible to social_messages.
"""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.smoke]


@pytest.fixture()
def dao(awm_workspace):
    from awm.social.dao import SocialDAO, init
    init()
    return SocialDAO()


def _inbound(account, **kw):
    from awm.social.connectors.base import InboundMessage
    return InboundMessage(account=account, platform="slack", **kw)


class _FakeConn:
    """Returns a fixed history list; records that history() was called."""

    def __init__(self, msgs, *, raise_unsupported=False):
        self._msgs = msgs
        self._raise = raise_unsupported
        self.calls = []

    async def history(self, channel, *, limit=50, before=None):
        self.calls.append((channel, limit, before))
        if self._raise:
            raise NotImplementedError("slack connector does not support history fetch")
        return self._msgs


@pytest.fixture()
def wired(dao, monkeypatch):
    """Point hub_adapter's module globals at the tmp DB + a fake connector."""
    import awm.social.hub_adapter as hub
    monkeypatch.setattr(hub, "_dao", dao)
    monkeypatch.setattr(hub, "_connectors", {})
    monkeypatch.setattr(hub, "_accounts", {})
    return hub


class TestHistoryHandler:
    async def test_persists_and_returns(self, wired, dao):
        msgs = [
            _inbound("s", channel_id="C1", sender_id="U1", sender_name="bob",
                     text="older", message_id="9.1", ts="9.1"),
            _inbound("s", channel_id="C1", sender_id="U2", sender_name="amy",
                     text="newer", message_id="9.2", ts="9.2"),
        ]
        wired._connectors["s"] = _FakeConn(msgs)

        out = await wired._h_history({"account": "s", "channel": "C1", "limit": 10})
        assert out["fetched"] == 2 and out["new"] == 2
        # Both rows persisted and visible via the normal poll.
        stored = dao.list_messages(channel="C1")
        assert [m["text"] for m in stored] == ["older", "newer"]
        assert all(m["direction"] == "in" for m in stored)
        # history() got the limit + (absent) before threaded through.
        assert wired._connectors["s"].calls == [("C1", 10, None)]

    async def test_refetch_is_idempotent(self, wired, dao):
        msgs = [_inbound("s", channel_id="C1", sender_id="U1", text="hi",
                         message_id="dup", ts="1")]
        wired._connectors["s"] = _FakeConn(msgs)

        first = await wired._h_history({"account": "s", "channel": "C1"})
        second = await wired._h_history({"account": "s", "channel": "C1"})
        assert first["new"] == 1
        assert second["new"] == 0 and second["fetched"] == 1  # deduped
        assert len(dao.list_messages(channel="C1")) == 1

    async def test_before_threaded_through(self, wired):
        wired._connectors["s"] = _FakeConn([])
        await wired._h_history(
            {"account": "s", "channel": "C1", "limit": 5, "before": "8.0"})
        assert wired._connectors["s"].calls == [("C1", 5, "8.0")]

    async def test_unknown_account_raises(self, wired):
        with pytest.raises(RuntimeError):
            await wired._h_history({"account": "nope", "channel": "C1"})

    async def test_unsupported_connector_surfaces(self, wired):
        wired._connectors["s"] = _FakeConn([], raise_unsupported=True)
        with pytest.raises(NotImplementedError):
            await wired._h_history({"account": "s", "channel": "C1"})


class _FakeChanConn:
    """Records list_channels(include_dms=) + open_dm() calls."""

    def __init__(self):
        self.list_calls = []
        self.open_calls = []

    async def list_channels(self, *, include_dms=False):
        from awm.social.connectors.base import Channel
        self.list_calls.append(include_dms)
        chans = [Channel(id="C1", name="general", kind="channel")]
        if include_dms:
            chans.append(Channel(id="D1", name="UALICE", kind="dm"))
        return chans

    async def open_dm(self, user):
        from awm.social.connectors.base import Channel
        self.open_calls.append(user)
        return Channel(id="D1", name=user, kind="dm")


class TestChannelsAndOpenDm:
    async def test_channels_default_excludes_dms(self, wired):
        wired._connectors["s"] = _FakeChanConn()
        out = await wired._h_channels({"account": "s"})
        assert [c["id"] for c in out["channels"]] == ["C1"]
        assert wired._connectors["s"].list_calls == [False]

    async def test_channels_include_dms(self, wired):
        wired._connectors["s"] = _FakeChanConn()
        out = await wired._h_channels({"account": "s", "include_dms": True})
        assert {c["id"] for c in out["channels"]} == {"C1", "D1"}
        assert {c["kind"] for c in out["channels"]} == {"channel", "dm"}
        assert wired._connectors["s"].list_calls == [True]

    async def test_open_dm_returns_channel(self, wired):
        wired._connectors["s"] = _FakeChanConn()
        out = await wired._h_open_dm({"account": "s", "user": "alice"})
        assert out["channel"] == {"id": "D1", "name": "alice", "kind": "dm"}
        assert wired._connectors["s"].open_calls == ["alice"]

    async def test_open_dm_unknown_account_raises(self, wired):
        with pytest.raises(RuntimeError):
            await wired._h_open_dm({"account": "nope", "user": "alice"})


class _FakeBackfillConn:
    """list_channels(include_dms=) + history() with one unreadable channel."""

    def __init__(self, unreadable=("BAD",)):
        self._unreadable = set(unreadable)
        self.history_channels = []

    async def list_channels(self, *, include_dms=False):
        from awm.social.connectors.base import Channel
        chans = [Channel(id="C1", name="general", kind="channel")]
        if include_dms:
            chans.append(Channel(id="D1", name="UALICE", kind="dm"))
        chans.append(Channel(id="BAD", name="team-channel", kind="channel"))
        return chans

    async def history(self, channel, *, limit=50, before=None):
        self.history_channels.append(channel)
        if channel in self._unreadable:
            raise RuntimeError(f"{channel}: history endpoint 404")
        return [_inbound("s", channel_id=channel, sender_id="U1",
                         text=f"msg in {channel}", message_id=f"{channel}.1",
                         ts="1")]


class TestBackfillHandler:
    async def test_backfill_covers_channels_and_dms(self, wired, dao):
        conn = _FakeBackfillConn()
        wired._connectors["s"] = conn
        out = await wired._h_backfill({"account": "s"})
        # include_dms defaults to True for backfill → C1, D1, BAD all enumerated.
        assert out["conversations"] == 3
        assert "D1" in conn.history_channels
        # The readable channels' messages are persisted + searchable.
        assert out["new"] == 2  # C1 + D1 (BAD failed)
        found = dao.search_messages(query="msg in D1")
        assert len(found) == 1

    async def test_backfill_reports_unreadable(self, wired):
        wired._connectors["s"] = _FakeBackfillConn(unreadable=("BAD",))
        out = await wired._h_backfill({"account": "s"})
        assert out["unreadable"] == 1
        bad = next(r for r in out["results"] if r["channel"] == "BAD")
        assert bad["ok"] is False and "404" in bad["error"]
        # A readable one is still marked ok with counts.
        good = next(r for r in out["results"] if r["channel"] == "C1")
        assert good["ok"] is True and good["new"] == 1

    async def test_backfill_can_exclude_dms(self, wired):
        conn = _FakeBackfillConn()
        wired._connectors["s"] = conn
        out = await wired._h_backfill({"account": "s", "include_dms": False})
        assert "D1" not in conn.history_channels
        assert out["conversations"] == 2  # C1 + BAD only

    async def test_backfill_unknown_account_raises(self, wired):
        with pytest.raises(RuntimeError):
            await wired._h_backfill({"account": "nope"})
