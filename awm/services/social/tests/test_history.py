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
