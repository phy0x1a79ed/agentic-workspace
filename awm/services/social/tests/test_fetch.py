"""Tests for the live fetch/search/download_attachments handlers.

These drive the hub-adapter handlers with fake connectors (no live platform):
``fetch`` and ``search`` query the connector directly and surface attachment
metadata; ``download_attachments`` writes the connector's bytes to a temp dir
OUTSIDE the workspace and returns the paths. Nothing is persisted.
"""

from __future__ import annotations

import os

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


def _att(idx, filename, **kw):
    from awm.social.connectors.base import Attachment
    return Attachment(idx=idx, filename=filename, **kw)


class _FakeConn:
    """Records fetch/search/download calls and returns canned results."""

    def __init__(self, *, fetch_msgs=None, search_msgs=None, files=None,
                 raise_unsupported=False):
        self._fetch = fetch_msgs or []
        self._search = search_msgs or []
        self._files = files or []
        self._raise = raise_unsupported
        self.fetch_calls = []
        self.search_calls = []
        self.download_calls = []

    async def fetch(self, channel, *, limit=50, before=None):
        self.fetch_calls.append((channel, limit, before))
        if self._raise:
            raise NotImplementedError("slack connector does not support fetch")
        return self._fetch

    async def search(self, query, *, limit=50, channel=None):
        self.search_calls.append((query, limit, channel))
        if self._raise:
            raise NotImplementedError("discord connector does not support search")
        return self._search

    async def download_attachments(self, channel, message_id, *, idx=None):
        self.download_calls.append((channel, message_id, idx))
        return self._files


@pytest.fixture()
def wired(dao, monkeypatch):
    """Point hub_adapter's module globals at the tmp DB + fake connectors."""
    import awm.social.hub_adapter as hub
    monkeypatch.setattr(hub, "_dao", dao)
    monkeypatch.setattr(hub, "_connectors", {})
    monkeypatch.setattr(hub, "_accounts", {})
    monkeypatch.setattr(hub, "_seen", set())
    return hub


class TestFetchHandler:
    async def test_returns_messages_with_attachments(self, wired):
        msgs = [
            _inbound("s", channel_id="C1", sender_id="U1", sender_name="bob",
                     text="older", message_id="9.1", ts="9.1"),
            _inbound("s", channel_id="C1", sender_id="U2", sender_name="amy",
                     text="manuscript", message_id="9.2", ts="9.2",
                     attachments=[_att(0, "draft.docx", mime="application/x", size=42)]),
        ]
        wired._connectors["s"] = _FakeConn(fetch_msgs=msgs)
        out = await wired._h_fetch({"account": "s", "channel": "C1", "limit": 10})
        assert out["count"] == 2
        assert [m["text"] for m in out["messages"]] == ["older", "manuscript"]
        att = out["messages"][1]["attachments"]
        assert att == [{"idx": 0, "filename": "draft.docx",
                        "mime": "application/x", "size": 42, "url": ""}]
        # limit + (absent) before threaded through; nothing persisted.
        assert wired._connectors["s"].fetch_calls == [("C1", 10, None)]

    async def test_before_threaded_through(self, wired):
        wired._connectors["s"] = _FakeConn()
        await wired._h_fetch(
            {"account": "s", "channel": "C1", "limit": 5, "before": "8.0"})
        assert wired._connectors["s"].fetch_calls == [("C1", 5, "8.0")]

    async def test_unknown_account_raises(self, wired):
        with pytest.raises(RuntimeError):
            await wired._h_fetch({"account": "nope", "channel": "C1"})

    async def test_unsupported_connector_surfaces(self, wired):
        wired._connectors["s"] = _FakeConn(raise_unsupported=True)
        with pytest.raises(NotImplementedError):
            await wired._h_fetch({"account": "s", "channel": "C1"})


class TestSearchHandler:
    async def test_search_queries_connector_live(self, wired):
        hits = [_inbound("s", channel_id="C1", sender_id="U2", sender_name="amy",
                         text="manuscript draft", message_id="9.2", ts="9.2")]
        wired._connectors["s"] = _FakeConn(search_msgs=hits)
        out = await wired._h_search(
            {"account": "s", "query": "manuscript", "limit": 20})
        assert out["count"] == 1
        assert out["messages"][0]["text"] == "manuscript draft"
        assert wired._connectors["s"].search_calls == [("manuscript", 20, None)]

    async def test_search_threads_channel(self, wired):
        wired._connectors["s"] = _FakeConn()
        await wired._h_search(
            {"account": "s", "query": "q", "channel": "general"})
        assert wired._connectors["s"].search_calls == [("q", 50, "general")]

    async def test_search_unsupported_surfaces(self, wired):
        wired._connectors["d"] = _FakeConn(raise_unsupported=True)
        with pytest.raises(NotImplementedError):
            await wired._h_search({"account": "d", "query": "x"})

    async def test_search_unknown_account_raises(self, wired):
        with pytest.raises(RuntimeError):
            await wired._h_search({"account": "nope", "query": "x"})


class TestDownloadHandler:
    async def test_writes_files_to_temp_dir_outside_awm(self, wired, awm_workspace):
        files = [("draft.docx", "application/vnd.x", b"PK\x03\x04 manuscript bytes")]
        wired._connectors["s"] = _FakeConn(files=files)
        out = await wired._h_download_attachments(
            {"account": "s", "channel": "D1", "message_id": "1782341432"})
        assert out["count"] == 1
        f = out["files"][0]
        assert f["filename"] == "draft.docx" and f["size"] == len(files[0][2])
        # The file really exists on disk with the right bytes …
        assert os.path.isfile(f["path"])
        with open(f["path"], "rb") as fh:
            assert fh.read() == files[0][2]
        # … and lives OUTSIDE the awm workspace (a system temp dir).
        awm_dir = str(awm_workspace["awm_dir"].resolve())
        assert not os.path.realpath(f["path"]).startswith(awm_dir)
        # idx flowed through as absent (whole message).
        assert wired._connectors["s"].download_calls == [("D1", "1782341432", None)]

    async def test_idx_selects_single_attachment(self, wired):
        wired._connectors["s"] = _FakeConn(files=[("a.bin", "x", b"a")])
        await wired._h_download_attachments(
            {"account": "s", "channel": "D1", "message_id": "m1", "idx": 2})
        assert wired._connectors["s"].download_calls == [("D1", "m1", 2)]

    async def test_no_attachments_returns_empty(self, wired):
        wired._connectors["s"] = _FakeConn(files=[])
        out = await wired._h_download_attachments(
            {"account": "s", "channel": "D1", "message_id": "m1"})
        assert out["count"] == 0 and out["files"] == [] and out["dir"] == ""

    async def test_unknown_account_raises(self, wired):
        with pytest.raises(RuntimeError):
            await wired._h_download_attachments(
                {"account": "nope", "channel": "D1", "message_id": "m1"})


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


class TestEmitNoPersist:
    async def test_on_message_emits_and_dedupes(self, wired, monkeypatch):
        emitted = []

        class _Adapter:
            async def emit(self, topic, payload):
                emitted.append((topic, payload))

        monkeypatch.setattr(wired, "_adapter", _Adapter())
        m = _inbound("s", channel_id="C1", sender_id="U1", sender_name="bob",
                     text="hi", message_id="9.1", ts="9.1",
                     attachments=[_att(0, "f.bin", mime="x", size=3)])
        await wired._on_message(m)
        await wired._on_message(m)  # redelivery — deduped
        assert len(emitted) == 1
        topic, payload = emitted[0]
        assert topic == "message"
        assert payload["text"] == "hi" and payload["direction"] == "in"
        assert payload["attachments"][0]["filename"] == "f.bin"
