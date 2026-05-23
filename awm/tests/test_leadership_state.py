"""Tests for leadership state-machine transitions, callback dispatch, and
per-peer probe bookkeeping (K=3 promote / K=1 demote)."""

from __future__ import annotations

import asyncio

import pytest

from awm.services.leadership import state as ldr_state


@pytest.fixture(autouse=True)
def _fresh_state():
    """Each test starts with a clean singleton."""
    ldr_state.reset_for_test()
    yield
    ldr_state.reset_for_test()


def test_configure_initializes_standby():
    ldr_state.configure("xps", 20)
    s = ldr_state.get_state()
    assert s.leadership == "STANDBY"
    assert s.self_peer_id == "xps"
    assert s.self_priority == 20
    assert s.current_leader is None


@pytest.mark.asyncio
async def test_promote_fires_callback():
    ldr_state.configure("xps", 20)
    fired = []
    ldr_state.on_promote(lambda: fired.append("p"))
    await ldr_state.promote()
    assert ldr_state.get_state().leadership == "ACTIVE"
    assert ldr_state.get_state().current_leader == "xps"
    assert fired == ["p"]


@pytest.mark.asyncio
async def test_promote_is_idempotent():
    ldr_state.configure("xps", 20)
    fired = []
    ldr_state.on_promote(lambda: fired.append("p"))
    await ldr_state.promote()
    await ldr_state.promote()
    assert fired == ["p"]  # second promote is a no-op


@pytest.mark.asyncio
async def test_demote_fires_callback_only_from_active():
    ldr_state.configure("xps", 20)
    demoted = []
    ldr_state.on_demote(lambda: demoted.append("d"))
    # STANDBY → STANDBY: no fire, but updates current_leader.
    await ldr_state.demote("capella")
    assert demoted == []
    assert ldr_state.get_state().current_leader == "capella"
    # ACTIVE → STANDBY: fires.
    await ldr_state.promote()
    await ldr_state.demote("capella")
    assert demoted == ["d"]


@pytest.mark.asyncio
async def test_async_callback_is_awaited():
    ldr_state.configure("xps", 20)
    fired = []

    async def cb():
        await asyncio.sleep(0)
        fired.append("async")

    ldr_state.on_promote(cb)
    await ldr_state.promote()
    assert fired == ["async"]


def test_record_probe_result_counts_consecutive_failures():
    ldr_state.configure("xps", 20)
    ldr_state.record_probe_result("capella", False)
    ldr_state.record_probe_result("capella", False)
    s = ldr_state.get_state()
    assert s.consecutive_fail["capella"] == 2
    # Success resets the counter.
    ldr_state.record_probe_result("capella", True)
    assert s.consecutive_fail["capella"] == 0


def test_has_recent_ok_under_threshold():
    ldr_state.configure("xps", 20)
    ldr_state.record_probe_result("capella", False)
    ldr_state.record_probe_result("capella", False)
    assert ldr_state.has_recent_ok("capella")  # K=2 < 3
    ldr_state.record_probe_result("capella", False)
    assert not ldr_state.has_recent_ok("capella")  # K=3 -> stale


def test_unseen_peer_is_considered_ok():
    """A peer we've never probed counts as 'recent OK' until probes start
    failing. Avoids spurious promotions during first poll round."""
    ldr_state.configure("xps", 20)
    assert ldr_state.has_recent_ok("never-probed")


@pytest.mark.asyncio
async def test_current_leader_base_url_self_returns_none(awm_workspace):
    ldr_state.configure("xps", 20)
    await ldr_state.promote()
    # When self is leader, no redirect needed → None.
    assert ldr_state.current_leader_base_url() is None


@pytest.mark.asyncio
async def test_current_leader_base_url_from_peers_table(awm_workspace):
    from awm.services.network import peers as peer_svc
    peer_svc.add_peer(
        "capella", "self",
        endpoints=[{"kind": "direct", "url": "https://10.0.0.1:7820"}],
        peer_priority=10,
    )
    ldr_state.configure("xps", 20)
    await ldr_state.demote("capella")
    assert ldr_state.current_leader_base_url() == "https://10.0.0.1:7820"


@pytest.mark.asyncio
async def test_callback_exception_does_not_break_transition():
    ldr_state.configure("xps", 20)

    def bad():
        raise RuntimeError("boom")

    ldr_state.on_promote(bad)
    await ldr_state.promote()  # must not raise
    assert ldr_state.get_state().leadership == "ACTIVE"
