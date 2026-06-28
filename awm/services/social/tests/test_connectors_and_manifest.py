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
            "social_send", "social_messages", "social_history", "social_search",
            "social_accounts", "social_channels", "social_operators",
            "social_operator_add", "social_operator_remove", "social_lookup",
        }
        # Every declared function has a handler.
        for f in fns:
            assert f["name"] in HANDLERS
        assert API_MANIFEST["emitters"] == [{"topic": "message"}]

    def test_send_requires_account_channel_text(self):
        from awm.social.hub_adapter import API_MANIFEST

        send = next(f for f in API_MANIFEST["functions"] if f["name"] == "send")
        required = {p["name"] for p in send["params"] if p["required"]}
        assert required == {"account", "channel", "text"}

    def test_history_and_search_required_params(self):
        from awm.social.hub_adapter import API_MANIFEST

        hist = next(f for f in API_MANIFEST["functions"] if f["name"] == "history")
        assert {p["name"] for p in hist["params"] if p["required"]} == {
            "account", "channel"}
        srch = next(f for f in API_MANIFEST["functions"] if f["name"] == "search")
        assert {p["name"] for p in srch["params"] if p["required"]} == {"query"}


class TestConnectorHistoryDefault:
    async def test_base_history_raises_not_implemented(self):
        from awm.social.connectors.base import Account, Connector

        class _Bare(Connector):
            platform = "bare"

            async def start(self): ...
            async def send(self, channel, text, *, thread=None): return {}
            async def list_channels(self): return []
            async def identity(self): ...
            async def close(self): ...

        c = _Bare(Account(name="x", platform="bare", token=""), _noop)
        with pytest.raises(NotImplementedError):
            await c.history("C1")
