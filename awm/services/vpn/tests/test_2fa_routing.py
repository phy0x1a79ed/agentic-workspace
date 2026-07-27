"""Where the pre-dial Duo burst gets armed.

`arm_2fa_burst` is the one 2fa consumer that used to bypass the
``AWM_TWOFA_PEER`` selector and POST its own gateway unconditionally. On a node
that borrows 2fa that meant this caller armed a local approver while `ssh` armed
the owner's — and the fleet-global Duo attempt budget only holds if every
consumer agrees on where the singleton lives. These tests pin the routing, and
pin that arming stays best-effort: a dial must still proceed (falling back to a
human phone tap) when 2fa is unreachable.
"""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.smoke]


@pytest.fixture()
def routed(monkeypatch):
    """Capture what arm_2fa_burst hands to the local-or-peer branch point."""
    from awm.vpn import container
    import awm.gatewayclient as gc

    calls: list[tuple] = []

    def _fake(peer, service, fn, args=None, **kw):
        calls.append((peer, service, fn, args))
        return {"ok": True}

    monkeypatch.setattr(gc, "call_sync_maybe_peer", _fake)
    return container, gc, calls


class TestArmRouting:
    def test_owner_node_arms_locally(self, routed, monkeypatch):
        container, gc, calls = routed
        monkeypatch.setattr(gc, "peer_env", lambda var: None)

        assert container.arm_2fa_burst("cwl") is True
        assert calls == [(None, "2fa", "burst", {"device": "cwl"})]

    def test_borrowing_node_arms_on_the_owner(self, routed, monkeypatch):
        """The regression this exists for: ssh armed mira, vpn armed itself."""
        container, gc, calls = routed
        monkeypatch.setattr(gc, "peer_env", lambda var: "mira")

        assert container.arm_2fa_burst("cwl") is True
        assert calls == [("mira", "2fa", "burst", {"device": "cwl"})]

    def test_it_reads_the_same_selector_ssh_reads(self, routed, monkeypatch):
        """Half-routing is prevented by both consumers reading one variable."""
        container, gc, calls = routed
        seen: list[str] = []

        def _peer_env(var):
            seen.append(var)
            return "mira"

        monkeypatch.setattr(gc, "peer_env", _peer_env)
        container.arm_2fa_burst("alliance")
        assert seen == ["AWM_TWOFA_PEER"]

    def test_unreachable_2fa_is_reported_not_raised(self, monkeypatch):
        """Arming is best-effort by contract — a dial must not die because the
        approver is down; it degrades to a manual Duo tap."""
        from awm.vpn import container
        import awm.gatewayclient as gc

        def _boom(peer, service, fn, args=None, **kw):
            raise gc.PeerError("ssh refused")

        monkeypatch.setattr(gc, "peer_env", lambda var: "mira")
        monkeypatch.setattr(gc, "call_sync_maybe_peer", _boom)

        assert container.arm_2fa_burst("cwl") is False


class TestSelectorTwin:
    """The sync twin must branch identically to the async one."""

    def test_falsy_peer_goes_local(self, monkeypatch):
        import awm.gatewayclient as gc
        seen: list[str] = []
        monkeypatch.setattr(gc, "call_sync",
                            lambda *a, **k: seen.append("local") or {"l": 1})
        monkeypatch.setattr(gc, "call_peer_sync",
                            lambda *a, **k: seen.append("peer") or {"p": 1})

        assert gc.call_sync_maybe_peer(None, "2fa", "burst", {}) == {"l": 1}
        assert gc.call_sync_maybe_peer("", "2fa", "burst", {}) == {"l": 1}
        assert seen == ["local", "local"]

    def test_named_peer_goes_remote(self, monkeypatch):
        import awm.gatewayclient as gc
        seen: list[str] = []
        monkeypatch.setattr(gc, "call_sync",
                            lambda *a, **k: seen.append("local") or {"l": 1})
        monkeypatch.setattr(gc, "call_peer_sync",
                            lambda *a, **k: seen.append("peer") or {"p": 1})

        assert gc.call_sync_maybe_peer("mira", "2fa", "burst", {}) == {"p": 1}
        assert seen == ["peer"]
