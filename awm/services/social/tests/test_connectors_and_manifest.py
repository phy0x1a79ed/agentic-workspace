"""Tests for the connector registry/factory and the hub manifest projection."""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.smoke]


class TestRegistry:
    def test_known_platforms_registered(self):
        from awm.social import connectors
        assert set(connectors.REGISTRY) == {"discord", "slack", "gmail"}

    def test_build_gmail_connector(self):
        from awm.social import connectors
        from awm.social.config import AccountConfig

        async def _noop(_m):
            return None

        g = connectors.build(
            AccountConfig(name="gmail-me", platform="gmail", token="app-pw",
                          address="me@gmail.com"), _noop)
        assert g.platform == "gmail"
        assert g.account.address == "me@gmail.com"


class TestGmailSubject:
    def test_explicit_subject_header_split(self):
        from awm.social.connectors.gmail_conn import _split_subject
        subj, body = _split_subject("Subject: Hi there\n\nthe body line")
        assert subj == "Hi there"
        assert body == "the body line"

    def test_no_header_uses_first_line(self):
        from awm.social.connectors.gmail_conn import _split_subject
        subj, body = _split_subject("just a plain message")
        assert subj == "just a plain message"
        assert body == "just a plain message"

    def test_build_returns_right_connector(self):
        from awm.social import connectors
        from awm.social.config import AccountConfig

        async def _noop(_m):
            return None

        d = connectors.build(
            AccountConfig(name="d", platform="discord", token="t"), _noop)
        s = connectors.build(
            AccountConfig(name="s", platform="slack", token="xoxb-x",
                          app_token="xapp-1"), _noop)
        assert d.platform == "discord" and d.account.name == "d"
        assert s.platform == "slack" and s.account.app_token == "xapp-1"

    def test_build_rejects_unknown_platform(self):
        from awm.social import connectors
        from awm.social.config import AccountConfig

        async def _noop(_m):
            return None

        with pytest.raises(ValueError):
            connectors.build(
                AccountConfig(name="x", platform="myspace", token="t"), _noop)


async def _noop(_m):
    return None


class _FakeWeb:
    """Stand-in AsyncWebClient for session-mode poll tests.

    ``conversations_history`` distinguishes the first-run baseline probe (no
    ``oldest`` kwarg, ``limit=1``) from a delta poll (``oldest=`` present).
    """

    def __init__(self, convs, baseline_msgs, delta_msgs):
        self._convs = convs
        self._baseline = baseline_msgs
        self._delta = delta_msgs
        self.calls = []

    async def users_conversations(self, **kw):
        return {"channels": [{"id": c} for c in self._convs]}

    async def conversations_history(self, channel, **kw):
        self.calls.append((channel, dict(kw)))
        if "oldest" not in kw:
            return {"messages": self._baseline.get(channel, [])}
        return {"messages": self._delta.get(channel, [])}


class TestSlackSession:
    def test_build_session_account_from_creds_cmd(self):
        from awm.social import connectors
        from awm.social.config import AccountConfig

        c = connectors.build(
            AccountConfig(name="slack-mira", platform="slack", token="",
                          creds_cmd="ssh mira awm-slack-creds"), _noop)
        assert c.platform == "slack"
        assert c._session is True
        assert c.account.creds_cmd == "ssh mira awm-slack-creds"

    def test_inline_xoxc_sets_cookie_header(self):
        from awm.social.connectors.base import Account
        from awm.social.connectors.slack_conn import SlackConnector

        c = SlackConnector(
            Account(name="s", platform="slack", token="xoxc-tok",
                    cookie="xoxd-ck"), _noop)
        assert c._session is True
        web = c._web_client()  # builds a real AsyncWebClient with the cookie header
        assert web.headers.get("Cookie") == "d=xoxd-ck"

    async def test_fetch_creds_runs_command(self):
        from awm.social.connectors.base import Account
        from awm.social.connectors.slack_conn import SlackConnector

        c = SlackConnector(
            Account(name="s", platform="slack", token="",
                    creds_cmd="printf '{\"token\":\"xoxc-AA\",\"cookie\":\"xoxd-BB\"}'"),
            _noop)
        await c._fetch_creds()
        assert c._token == "xoxc-AA"
        assert c._cookie == "xoxd-BB"
        assert c._web is None  # cache cleared so the next call rebuilds

    async def test_fetch_creds_nonzero_raises(self):
        from awm.social.connectors.base import Account
        from awm.social.connectors.slack_conn import SlackConnector

        c = SlackConnector(
            Account(name="s", platform="slack", token="", creds_cmd="exit 2"),
            _noop)
        with pytest.raises(RuntimeError):
            await c._fetch_creds()

    async def test_poll_baselines_then_emits_only_new(self):
        from awm.social.connectors.base import Account
        from awm.social.connectors.slack_conn import SlackConnector

        received = []

        async def on_msg(m):
            received.append(m)

        c = SlackConnector(
            Account(name="s", platform="slack", token="", creds_cmd="x"), on_msg)
        c._self_id = "UME"
        # delta is newest-first like the real API: a system join, our own echo,
        # then two real inbound messages — all with ts > the 100.0 baseline.
        c._web = _FakeWeb(
            convs=["D1"],
            baseline_msgs={"D1": [{"ts": "100.0"}]},
            delta_msgs={"D1": [
                {"ts": "104.0", "subtype": "channel_join", "text": "joined"},
                {"ts": "103.0", "user": "UME", "text": "my own send"},
                {"ts": "102.0", "user": "UOTHER", "text": "hi"},
                {"ts": "101.0", "user": "UOTHER", "text": "older"},
            ]},
        )

        baseline = {}
        await c._poll_once(baseline, first=True)
        assert received == []              # first run never replays history
        assert baseline["D1"] == "100.0"

        await c._poll_once(baseline, first=False)
        assert [m.text for m in received] == ["older", "hi"]  # oldest-first, ours/system dropped
        assert baseline["D1"] == "104.0"   # advances past skipped echo/system too


class TestManifest:
    def test_social_tools_projected(self):
        from awm.social.hub_adapter import API_MANIFEST, HANDLERS

        fns = API_MANIFEST["functions"]
        tools = {f.get("tool", f["name"]) for f in fns}
        assert tools == {
<<<<<<< HEAD
            "social_send", "social_messages", "social_history", "social_search",
            "social_accounts", "social_channels", "social_operators",
            "social_operator_add", "social_operator_remove", "social_lookup",
=======
            "social_send", "social_fetch", "social_search",
            "social_download_attachments", "social_accounts", "social_channels",
            "social_open_dm", "social_operators", "social_operator_add",
            "social_operator_remove", "social_lookup",
>>>>>>> feat/svc-social
        }
        # Every declared function has a handler.
        for f in fns:
            assert f["name"] in HANDLERS
        # Inbound messages + slash-command invocations are both emitted.
        assert API_MANIFEST["emitters"] == [
            {"topic": "message"}, {"topic": "command"}]

    def test_send_requires_account_channel_text(self):
        from awm.social.hub_adapter import API_MANIFEST

        send = next(f for f in API_MANIFEST["functions"] if f["name"] == "send")
        required = {p["name"] for p in send["params"] if p["required"]}
        assert required == {"account", "channel", "text"}

<<<<<<< HEAD
    def test_history_and_search_required_params(self):
        from awm.social.hub_adapter import API_MANIFEST

        hist = next(f for f in API_MANIFEST["functions"] if f["name"] == "history")
        assert {p["name"] for p in hist["params"] if p["required"]} == {
            "account", "channel"}
        srch = next(f for f in API_MANIFEST["functions"] if f["name"] == "search")
        assert {p["name"] for p in srch["params"] if p["required"]} == {"query"}


class TestConnectorHistoryDefault:
    async def test_base_history_raises_not_implemented(self):
=======
    def test_fetch_and_search_required_params(self):
        from awm.social.hub_adapter import API_MANIFEST

        fetch = next(f for f in API_MANIFEST["functions"] if f["name"] == "fetch")
        assert {p["name"] for p in fetch["params"] if p["required"]} == {
            "account", "channel"}
        srch = next(f for f in API_MANIFEST["functions"] if f["name"] == "search")
        assert {p["name"] for p in srch["params"] if p["required"]} == {
            "account", "query"}

    def test_download_attachments_params_and_timeout(self):
        from awm.social.hub_adapter import API_MANIFEST

        dl = next(f for f in API_MANIFEST["functions"]
                  if f["name"] == "download_attachments")
        assert {p["name"] for p in dl["params"] if p["required"]} == {
            "account", "channel", "message_id"}
        # Large files: a generous per-function timeout overrides the 30s RPC default.
        assert dl.get("timeout", 0) >= 120


class TestConnectorDefaults:
    def _bare(self):
>>>>>>> feat/svc-social
        from awm.social.connectors.base import Account, Connector

        class _Bare(Connector):
            platform = "bare"

            async def start(self): ...
            async def send(self, channel, text, *, thread=None): return {}
<<<<<<< HEAD
            async def list_channels(self): return []
=======
            async def list_channels(self, *, include_dms=False): return []
            async def identity(self): ...
            async def close(self): ...

        return _Bare(Account(name="x", platform="bare", token=""), _noop)

    async def test_base_fetch_raises_not_implemented(self):
        with pytest.raises(NotImplementedError):
            await self._bare().fetch("C1")

    async def test_base_search_raises_not_implemented(self):
        with pytest.raises(NotImplementedError):
            await self._bare().search("q")

    async def test_base_download_raises_not_implemented(self):
        with pytest.raises(NotImplementedError):
            await self._bare().download_attachments("C1", "m1")

    async def test_base_open_dm_raises_not_implemented(self):
        from awm.social.connectors.base import Account, Connector

        class _Bare(Connector):
            platform = "bare"

            async def start(self): ...
            async def send(self, channel, text, *, thread=None): return {}
            async def list_channels(self, *, include_dms=False): return []
>>>>>>> feat/svc-social
            async def identity(self): ...
            async def close(self): ...

        c = _Bare(Account(name="x", platform="bare", token=""), _noop)
        with pytest.raises(NotImplementedError):
<<<<<<< HEAD
            await c.history("C1")
=======
            await c.open_dm("U123")


class _FakeDmWeb:
    """Stand-in AsyncWebClient for the Slack DM-exposure tests: channel/DM
    enumeration, user resolution (paged), and conversations.open."""

    def __init__(self):
        self.calls = []

    async def users_conversations(self, **kw):
        self.calls.append(("users_conversations", dict(kw)))
        return {"channels": [
            {"id": "C1", "name": "general", "is_channel": True},
            {"id": "G1", "name": "secret", "is_private": True},
            {"id": "D1", "user": "UALICE", "is_im": True},
            {"id": "M1", "name": "mpdm-a--b", "is_mpim": True},
        ]}

    async def conversations_list(self, **kw):
        self.calls.append(("conversations_list", dict(kw)))
        return {"channels": [{"id": "C1", "name": "general"}]}

    async def users_list(self, **kw):
        self.calls.append(("users_list", dict(kw)))
        # Two pages, to exercise cursor paging.
        if not kw.get("cursor"):
            return {"members": [{"id": "UME", "name": "me"}],
                    "response_metadata": {"next_cursor": "pg2"}}
        return {"members": [
            {"id": "UALICE", "name": "alice",
             "profile": {"display_name": "Alice A"}}],
                "response_metadata": {"next_cursor": ""}}

    async def conversations_open(self, **kw):
        self.calls.append(("conversations_open", dict(kw)))
        return {"channel": {"id": "D1"}}


class TestSlackDms:
    def _conn(self, web):
        from awm.social.connectors.base import Account
        from awm.social.connectors.slack_conn import SlackConnector
        c = SlackConnector(
            Account(name="s", platform="slack", token="xoxc-x", cookie="c"),
            _noop)
        c._web = web
        return c

    async def test_list_channels_excludes_dms_by_default(self):
        web = _FakeDmWeb()
        chans = await self._conn(web).list_channels()
        assert web.calls[0][0] == "conversations_list"
        assert [c.id for c in chans] == ["C1"]

    async def test_list_channels_include_dms_enumerates_ims(self):
        web = _FakeDmWeb()
        chans = await self._conn(web).list_channels(include_dms=True)
        assert web.calls[0][0] == "users_conversations"
        kinds = {c.id: c.kind for c in chans}
        assert kinds == {"C1": "channel", "G1": "private",
                         "D1": "dm", "M1": "group"}
        # An im has no name → falls back to the peer user id.
        assert next(c.name for c in chans if c.id == "D1") == "UALICE"

    async def test_open_dm_by_user_id(self):
        web = _FakeDmWeb()
        ch = await self._conn(web).open_dm("UALICE")
        assert ch.id == "D1" and ch.kind == "dm"
        # A bare id needs no users.list lookup.
        assert not any(c[0] == "users_list" for c in web.calls)
        assert ("conversations_open", {"users": "UALICE"}) in web.calls

    async def test_open_dm_resolves_name_via_users_list(self):
        web = _FakeDmWeb()
        ch = await self._conn(web).open_dm("alice")
        assert ch.id == "D1"
        # Resolved the name across paged users.list, then opened by id.
        assert any(c[0] == "users_list" for c in web.calls)
        assert ("conversations_open", {"users": "UALICE"}) in web.calls

    async def test_open_dm_unknown_name_raises(self):
        web = _FakeDmWeb()
        with pytest.raises(RuntimeError):
            await self._conn(web).open_dm("nobody")
>>>>>>> feat/svc-social
