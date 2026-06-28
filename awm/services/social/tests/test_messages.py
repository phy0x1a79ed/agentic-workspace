"""Tests for the social_messages table: record, dedupe, and poll."""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.smoke]


@pytest.fixture()
def dao(awm_workspace):
    from awm.social.dao import SocialDAO, init
    init()
    return SocialDAO()


class TestRecordMessage:
    def test_records_inbound(self, dao):
        row = dao.record_message(
            account="slack-bot", platform="slack", direction="in",
            channel_id="C1", sender_id="U1", text="hi", message_id="m1")
        assert row is not None and row["id"] > 0
        assert row["direction"] == "in"

    def test_dedupes_redelivery(self, dao):
        first = dao.record_message(
            account="slack-bot", platform="slack", direction="in",
            channel_id="C1", text="hi", message_id="dup")
        again = dao.record_message(
            account="slack-bot", platform="slack", direction="in",
            channel_id="C1", text="hi", message_id="dup")
        assert first is not None
        assert again is None  # deduped on (platform, account, message_id)

    def test_empty_message_id_not_deduped(self, dao):
        a = dao.record_message(
            account="slack-bot", platform="slack", direction="in",
            channel_id="C1", text="one", message_id="")
        b = dao.record_message(
            account="slack-bot", platform="slack", direction="in",
            channel_id="C1", text="two", message_id="")
        assert a is not None and b is not None and a["id"] != b["id"]

    def test_rejects_bad_direction(self, dao):
        with pytest.raises(ValueError):
            dao.record_message(
                account="x", platform="slack", direction="sideways")


class TestListMessages:
    def test_filters_and_orders_oldest_first(self, dao):
        dao.record_message(account="a", platform="slack", direction="out",
                           channel_id="C1", text="first", message_id="1")
        dao.record_message(account="a", platform="slack", direction="in",
                           channel_id="C1", text="second", message_id="2")
        dao.record_message(account="b", platform="discord", direction="in",
                           channel_id="C9", text="other", message_id="3")

        slack = dao.list_messages(platform="slack")
        assert [m["text"] for m in slack] == ["first", "second"]

        chan = dao.list_messages(channel="C9")
        assert len(chan) == 1 and chan[0]["text"] == "other"

    def test_limit_caps_results(self, dao):
        for i in range(5):
            dao.record_message(account="a", platform="slack", direction="in",
                               channel_id="C1", text=str(i), message_id=str(i))
        assert len(dao.list_messages(limit=3)) == 3
