"""Keep peer tests off the local gateway: no test here signs a real node token.

Outbound peer calls try a signed token first, which asks the local ``auth``
service. By default that fails, so the legacy-bearer path the older tests pin
runs unchanged. ``test_invoke_peer.py`` restores the real signers.
"""

from __future__ import annotations

import pytest

from awm import gatewayclient as gc


@pytest.fixture(autouse=True)
def _no_token_signing(monkeypatch):
    async def refuse(peer, *, force=False):
        raise gc.PeerError("token signing is off in this test")

    def refuse_sync(peer, *, force=False):
        raise gc.PeerError("token signing is off in this test")

    gc._peer_token_cache.clear()
    gc._peer_token_refused.clear()
    monkeypatch.setattr(gc, "peer_token", refuse)
    monkeypatch.setattr(gc, "peer_token_sync", refuse_sync)
