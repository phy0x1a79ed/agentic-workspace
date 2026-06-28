"""Tests for the connector registry/factory and the hub manifest projection."""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.smoke]


class TestRegistry:
    def test_known_platforms_registered(self):
        from awm.social import connectors
        assert set(connectors.REGISTRY) == {"discord", "slack"}

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


class TestManifest:
    def test_eight_social_tools_projected(self):
        from awm.social.hub_adapter import API_MANIFEST, HANDLERS

        fns = API_MANIFEST["functions"]
        tools = {f.get("tool", f["name"]) for f in fns}
        assert tools == {
            "social_send", "social_messages", "social_accounts",
            "social_channels", "social_operators", "social_operator_add",
            "social_operator_remove", "social_lookup",
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
