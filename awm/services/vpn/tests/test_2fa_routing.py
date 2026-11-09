"""Arming the pre-dial Duo burst stays best-effort: a dial must still proceed,
falling back to a human phone tap, when 2fa is unreachable."""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.smoke]


class TestArm:
    def test_unreachable_2fa_is_reported_not_raised(self, monkeypatch):
        """Arming is best-effort by contract — a dial must not die because the
        approver is down; it degrades to a manual Duo tap."""
        from awm.vpn import container
        import awm.gatewayclient as gc

        def _boom(peer, service, fn, args=None, **kw):
            raise gc.GatewayCallError(502, "ssh refused")

        monkeypatch.setattr(gc, "call_sync", lambda tool, verb, args=None, *, peer=None, **kw: (_boom)(peer, tool, verb, args, **kw))

        assert container.arm_2fa_burst("cwl") is False
