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

    monkeypatch.setattr(gc, "call_sync", lambda tool, verb, args=None, *, peer=None, **kw: (_fake)(peer, tool, verb, args, **kw))
    return container, gc, calls


class TestArmRouting:
    def test_borrowing_node_arms_on_the_owner(self, routed, monkeypatch):
        """The regression this exists for: ssh armed mira, vpn armed itself."""
        container, gc, calls = routed
        monkeypatch.setattr(gc, "peer_env", lambda var: "mira")

        assert container.arm_2fa_burst("cwl") is True
        assert calls == [("mira", "2fa", "burst", {"device": "cwl"})]

    def test_unreachable_2fa_is_reported_not_raised(self, monkeypatch):
        """Arming is best-effort by contract — a dial must not die because the
        approver is down; it degrades to a manual Duo tap."""
        from awm.vpn import container
        import awm.gatewayclient as gc

        def _boom(peer, service, fn, args=None, **kw):
            raise gc.GatewayCallError(502, "ssh refused")

        monkeypatch.setattr(gc, "peer_env", lambda var: "mira")
        monkeypatch.setattr(gc, "call_sync", lambda tool, verb, args=None, *, peer=None, **kw: (_boom)(peer, tool, verb, args, **kw))

        assert container.arm_2fa_burst("cwl") is False
