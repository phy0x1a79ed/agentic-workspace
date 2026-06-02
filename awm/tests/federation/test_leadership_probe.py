"""Probe-loop integration tests with mocked httpx transport.

Simulates 2-peer scenarios: kill the higher-priority peer, watch self promote
within K=3 rounds; restore it, watch self demote on the next round.
"""

from __future__ import annotations


import pytest
pytestmark = [pytest.mark.federation, pytest.mark.slow]

from unittest.mock import patch

import httpx
import pytest

from awm.services.leadership import state as ldr_state
from awm.services.leadership import poll as ldr_poll


@pytest.fixture(autouse=True)
def _fresh_state():
    ldr_state.reset_for_test()
    yield
    ldr_state.reset_for_test()


def _seed_two_peers(awm_workspace, *, capella_endpoint: str = "https://10.0.0.1:7820"):
    """Self = xps (priority 20). Higher-priority peer = capella (priority 10)."""
    from awm.services.network import peers as peer_svc

    # The local peer identity file is what get_local_identity reads.
    import json as _json
    from awm import config
    (config.AWM_DIR).mkdir(exist_ok=True, parents=True)
    config.PEER_FILE.write_text(_json.dumps({"peer_id": "xps"}) + "\n")

    # Self-row in peers table (so list_remote_peers can exclude us).
    peer_svc.add_peer(
        "xps", "self",
        endpoints=[{"kind": "direct", "url": "https://127.0.0.1:7820"}],
        peer_priority=20,
    )
    # Higher-priority peer.
    peer_svc.add_peer(
        "capella", "self",
        endpoints=[{"kind": "direct", "url": capella_endpoint}],
        peer_priority=10,
    )
    # Token for capella so federation._resolve can return one.
    token_dir = config.AWM_DIR / "peers"
    token_dir.mkdir(parents=True, exist_ok=True)
    (token_dir / "capella.token").write_text("capella-token\n")

    ldr_state.configure("xps", 20)


class _MockTransport(httpx.AsyncBaseTransport):
    """Returns the same response for every request unless raise_for_url
    matches the URL, in which case raises ConnectError."""

    def __init__(self, status_code: int = 200, *, raise_always: bool = False):
        self.status_code = status_code
        self.raise_always = raise_always
        self.calls: list[str] = []

    async def handle_async_request(self, request):
        self.calls.append(str(request.url))
        if self.raise_always:
            raise httpx.ConnectError("simulated unreachable")
        return httpx.Response(
            self.status_code,
            json={
                "status": "ok",
                "workspace_root": "/tmp",
                "leadership_state": "ACTIVE",
                "current_leader": "capella",
                "peer_priority": 10,
            },
            request=request,
        )


def _patch_transport(transport: _MockTransport):
    """Make every httpx.AsyncClient instantiation use our transport."""
    real_init = httpx.AsyncClient.__init__

    def patched_init(self, *args, **kwargs):
        kwargs["transport"] = transport
        return real_init(self, *args, **kwargs)

    return patch.object(httpx.AsyncClient, "__init__", patched_init)


async def test_higher_peer_up_keeps_self_standby(awm_workspace):
    _seed_two_peers(awm_workspace)
    transport = _MockTransport(status_code=200)
    with _patch_transport(transport):
        await ldr_poll.evaluate_once()
    s = ldr_state.get_state()
    assert s.leadership == "STANDBY"
    assert s.current_leader == "capella"


async def test_higher_peer_down_promotes_after_K_rounds(awm_workspace):
    _seed_two_peers(awm_workspace)
    transport = _MockTransport(raise_always=True)
    with _patch_transport(transport):
        # K=3 failures needed; first two rounds stay STANDBY.
        await ldr_poll.evaluate_once()
        assert ldr_state.get_state().leadership == "STANDBY"
        await ldr_poll.evaluate_once()
        assert ldr_state.get_state().leadership == "STANDBY"
        await ldr_poll.evaluate_once()
        assert ldr_state.get_state().leadership == "ACTIVE"
        assert ldr_state.get_state().current_leader == "xps"


async def test_higher_peer_restored_demotes_immediately(awm_workspace):
    _seed_two_peers(awm_workspace)
    # Promote first.
    down = _MockTransport(raise_always=True)
    with _patch_transport(down):
        for _ in range(3):
            await ldr_poll.evaluate_once()
    assert ldr_state.get_state().leadership == "ACTIVE"

    # Restore higher-priority peer; single success demotes.
    up = _MockTransport(status_code=200)
    with _patch_transport(up):
        await ldr_poll.evaluate_once()
    assert ldr_state.get_state().leadership == "STANDBY"
    assert ldr_state.get_state().current_leader == "capella"


async def test_no_higher_priority_peers_is_immediately_active(awm_workspace):
    """When self is the lowest priority value visible, no probes needed —
    promote unconditionally."""
    from awm.services.network import peers as peer_svc
    from awm import config
    import json as _json
    config.PEER_FILE.write_text(_json.dumps({"peer_id": "xps"}) + "\n")
    peer_svc.add_peer("xps", "self", peer_priority=0)
    # Add a strictly higher (worse) peer.
    peer_svc.add_peer("capella", "self", peer_priority=10)
    ldr_state.configure("xps", 0)
    await ldr_poll.evaluate_once()
    s = ldr_state.get_state()
    assert s.leadership == "ACTIVE"
    assert s.current_leader == "xps"
