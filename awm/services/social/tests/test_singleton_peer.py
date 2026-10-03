"""Fleet-singleton accounts: owned by one node, callable from every node.

An account marked ``singleton = true`` is one session for the whole fleet (a
Discord bot token). The node that leaves ``AWM_SOCIAL_PEER`` unset owns it and
connects it; a node that sets the selector connects nothing and forwards any
verb naming it to the owner. These tests pin both halves, plus the two ways the
wrapper could go wrong: forwarding on the owner (a loop) and changing how the
adapter runs a handler.
"""

from __future__ import annotations

import asyncio
import inspect

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.smoke]


def _write(path, text):
    path.write_text(text)
    return path


class TestSingletonConfig:
    def test_singleton_defaults_false(self, tmp_path):
        from awm.social import config as cfg
        p = _write(tmp_path / "social.toml", """
[account.discord-bot]
platform = "discord"
token = "dtoken"
""")
        assert cfg.load(p)[0].singleton is False

    def test_singleton_parsed(self, tmp_path):
        from awm.social import config as cfg
        p = _write(tmp_path / "social.toml", """
[account.discord-bot]
platform = "discord"
token = "dtoken"
singleton = true
""")
        assert cfg.load(p)[0].singleton is True

    def test_non_bool_singleton_is_rejected(self, tmp_path):
        from awm.social import config as cfg
        p = _write(tmp_path / "social.toml", """
[account.discord-bot]
platform = "discord"
token = "dtoken"
singleton = "yes"
""")
        with pytest.raises(cfg.SocialConfigError, match="singleton must be a boolean"):
            cfg.load(p)


class TestForwarding:
    """The wrapper around the six account-taking verbs."""

    @staticmethod
    def _patch(monkeypatch, ha, *, peer, connectors, peer_result=None,
               peer_exc=None):
        calls: list[tuple] = []

        async def _fake_call_peer(p, service, fn, args=None, **kw):
            calls.append((p, service, fn, args))
            if peer_exc is not None:
                raise peer_exc
            return peer_result

        import awm.gatewayclient as gc
        monkeypatch.setattr(gc, "peer_env", lambda var: peer)
        monkeypatch.setattr(gc, "call_peer", _fake_call_peer)
        monkeypatch.setattr(ha, "_connectors", connectors)
        return calls

    def test_borrower_forwards_an_account_it_does_not_hold(self, monkeypatch):
        from awm.social import hub_adapter as ha

        async def _local(args):
            raise AssertionError("local handler must not run for a borrowed account")

        calls = self._patch(monkeypatch, ha, peer="mira", connectors={},
                            peer_result={"ok": True, "id": "42"})
        wrapped = ha._forwarding("send", _local)
        out = asyncio.run(wrapped({"account": "discord-bot", "channel": "c",
                                   "text": "hi"}))

        assert out == {"ok": True, "id": "42"}
        assert calls == [("mira", "social", "send",
                          {"account": "discord-bot", "channel": "c", "text": "hi"})]

    def test_owner_never_forwards_even_for_an_unknown_account(self, monkeypatch):
        """No selector ⇒ this node owns the singletons ⇒ a typo fails locally.

        Keying on the selector rather than on "the account is missing" is what
        makes a forward loop impossible and keeps a typo'd name a local error.
        """
        from awm.social import hub_adapter as ha
        ran = []

        async def _local(args):
            ran.append(args)
            return {"local": True}

        calls = self._patch(monkeypatch, ha, peer=None, connectors={})
        wrapped = ha._forwarding("send", _local)
        out = asyncio.run(wrapped({"account": "typo", "channel": "c", "text": "x"}))

        assert out == {"local": True}
        assert ran and calls == []

    def test_locally_connected_account_stays_local_on_a_borrower(self, monkeypatch):
        from awm.social import hub_adapter as ha

        async def _local(args):
            return {"local": True}

        calls = self._patch(monkeypatch, ha, peer="mira",
                            connectors={"gmail": object()})
        wrapped = ha._forwarding("send", _local)
        out = asyncio.run(wrapped({"account": "gmail", "channel": "c", "text": "x"}))

        assert out == {"local": True}
        assert calls == []

    def test_unreachable_peer_names_the_owner(self, monkeypatch):
        """"Could not reach the owner" and "the owner refused" have different
        fixes, so they must not collapse into one message."""
        from awm.social import hub_adapter as ha
        import awm.gatewayclient as gc

        async def _local(args):
            raise AssertionError("must not run")

        self._patch(monkeypatch, ha, peer="mira", connectors={},
                    peer_exc=gc.PeerError("no ca"))
        wrapped = ha._forwarding("send", _local)
        with pytest.raises(RuntimeError, match="owned by peer 'mira'"):
            asyncio.run(wrapped({"account": "discord-bot", "channel": "c",
                                 "text": "x"}))

    def test_wrapper_preserves_how_the_adapter_runs_the_handler(self):
        """The adapter branches on ``iscoroutinefunction`` and reads arity.

        A wrapper that broke either would silently move DB work onto the event
        loop or drop the caller-identity argument.
        """
        from awm.social import hub_adapter as ha

        async def _local(args):
            return {}

        wrapped = ha._forwarding("send", _local)
        assert inspect.iscoroutinefunction(wrapped)
        assert len(inspect.signature(wrapped).parameters) == 1

    def test_only_account_taking_verbs_are_wrapped(self):
        """Wrapping the sync handlers would move their DB work onto the loop."""
        from awm.social import hub_adapter as ha

        forwarded = {"send", "channels", "open_dm", "fetch", "search",
                     "download_attachments"}
        for name in forwarded:
            assert inspect.iscoroutinefunction(ha.HANDLERS[name]), name
        # These take no account and must keep running in the worker thread.
        for name in ("list_operators", "add_operator", "remove_operator",
                     "lookup", "buckets"):
            assert not inspect.iscoroutinefunction(ha.HANDLERS[name]), name


class TestAccountsListing:
    def test_borrower_merges_the_owners_accounts(self, monkeypatch):
        from awm.social import hub_adapter as ha
        from awm.social import config as social_config
        import awm.gatewayclient as gc

        local_cfg = social_config.AccountConfig(
            name="gmail", platform="gmail", token="t", address="a@b.c")
        monkeypatch.setattr(ha, "_accounts", {"gmail": local_cfg})
        monkeypatch.setattr(ha, "_connectors", {"gmail": object()})
        monkeypatch.setattr(gc, "peer_env", lambda var: "mira")

        async def _fake_call_peer(p, service, fn, args=None, **kw):
            return {"accounts": [
                {"name": "discord-bot", "platform": "discord", "live": True},
                {"name": "gmail", "platform": "gmail", "live": True},
            ]}

        monkeypatch.setattr(gc, "call_peer", _fake_call_peer)
        out = asyncio.run(ha._h_accounts({}))

        by_name = {a["name"]: a for a in out["accounts"]}
        assert by_name["gmail"]["owner"] == "local"      # local wins over peer
        assert by_name["discord-bot"]["owner"] == "mira"  # peer-only is merged
        assert out["peer"] == "mira"

    def test_a_borrowed_singleton_reports_its_real_owner(self, monkeypatch):
        """Configured here, owned there.

        The account is in this node's social.toml but deliberately unconnected,
        so reporting owner=local would be a lie in exactly the case a caller
        most needs the truth — and its liveness is the peer's, not ours.
        """
        from awm.social import hub_adapter as ha
        from awm.social import config as social_config
        import awm.gatewayclient as gc

        cfg = social_config.AccountConfig(
            name="discord-bot", platform="discord", token="t", singleton=True)
        monkeypatch.setattr(ha, "_accounts", {"discord-bot": cfg})
        monkeypatch.setattr(ha, "_connectors", {})   # borrowed ⇒ not connected
        monkeypatch.setattr(gc, "peer_env", lambda var: "mira")

        async def _peer_says_live(p, service, fn, args=None, **kw):
            return {"accounts": [{"name": "discord-bot", "platform": "discord",
                                  "live": True}]}

        monkeypatch.setattr(gc, "call_peer", _peer_says_live)
        acc = asyncio.run(ha._h_accounts({}))["accounts"][0]

        assert acc["owner"] == "mira"
        assert acc["live"] is True    # the peer's session is the live one

    def test_unreachable_peer_still_lists_local_accounts(self, monkeypatch):
        from awm.social import hub_adapter as ha
        from awm.social import config as social_config
        import awm.gatewayclient as gc

        local_cfg = social_config.AccountConfig(
            name="gmail", platform="gmail", token="t", address="a@b.c")
        monkeypatch.setattr(ha, "_accounts", {"gmail": local_cfg})
        monkeypatch.setattr(ha, "_connectors", {"gmail": object()})
        monkeypatch.setattr(gc, "peer_env", lambda var: "mira")

        async def _boom(p, service, fn, args=None, **kw):
            raise gc.PeerError("ssh refused")

        monkeypatch.setattr(gc, "call_peer", _boom)
        out = asyncio.run(ha._h_accounts({}))

        assert [a["name"] for a in out["accounts"]] == ["gmail"]
        assert "ssh refused" in out["peer_error"]

    def test_owner_listing_has_no_peer_key(self, monkeypatch):
        from awm.social import hub_adapter as ha
        from awm.social import config as social_config
        import awm.gatewayclient as gc

        cfg = social_config.AccountConfig(
            name="discord-bot", platform="discord", token="t", singleton=True)
        monkeypatch.setattr(ha, "_accounts", {"discord-bot": cfg})
        monkeypatch.setattr(ha, "_connectors", {"discord-bot": object()})
        monkeypatch.setattr(gc, "peer_env", lambda var: None)

        out = asyncio.run(ha._h_accounts({}))
        assert "peer" not in out
        assert out["accounts"][0]["singleton"] is True
        assert out["accounts"][0]["live"] is True
