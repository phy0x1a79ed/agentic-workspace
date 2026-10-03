"""The ready-ASAP contract: register and signal ready, THEN initialise.

``ServiceAdapter.run`` used to await ``on_start`` in full before it registered,
so everything a service did at startup happened while it was invisible to the
gateway — and "not ready" meant either "broken" or "still loading", which is
no signal at all. The gateway now reaps a lease-holder that stays unready past
a grace, so that ambiguity is no longer survivable.

The inversion is only safe because inbound envelopes buffer: a call that lands
during initialisation waits for it rather than being answered against a
half-built service (``on_start`` is where ``init_service_db`` runs). The wait
is bounded so a hung init fails loudly instead of freezing every caller.

Sync tests driving async work via ``asyncio.run`` — this dist carries no
pytest-asyncio config.
"""

from __future__ import annotations

import asyncio

import pytest

from awm.gatewayclient import adapter as adapter_mod
from awm.gatewayclient.adapter import ServiceAdapter


def _adapter(**kw) -> ServiceAdapter:
    return ServiceAdapter("svc", {"functions": [{"name": "ping"}]},
                          {"ping": lambda args: "pong"}, **kw)


def test_registration_does_not_wait_for_a_slow_on_start(monkeypatch):
    """The whole point: a service with a 10s init is visible to the gateway in
    milliseconds, not in 10 seconds."""
    order: list[str] = []

    async def slow_init():
        order.append("init-start")
        await asyncio.sleep(0.2)
        order.append("init-done")

    ad = _adapter(on_start=slow_init)

    async def fake_run_target(hub_url, sid, *, name, prefix, overlay):
        order.append("registered")

    monkeypatch.setattr(ad, "_run_target", fake_run_target)
    monkeypatch.setenv("AWM_HUB_URL", "http://127.0.0.1:7819")
    monkeypatch.delenv("AWM_SERVICE_ID", raising=False)

    asyncio.run(ad.run())

    assert order.index("registered") < order.index("init-done")


def test_a_call_during_init_is_buffered_not_failed():
    ad = _adapter()
    ad._init_started = True

    async def go():
        # Init is running but has not finished — the gate is closed.
        task = asyncio.create_task(ad._dispatch("ping", {}, None))
        await asyncio.sleep(0.05)
        assert not task.done()          # buffered, not answered, not failed
        ad._init_done.set()
        return await task

    assert asyncio.run(go()) == "pong"


def test_a_call_after_init_is_not_delayed():
    ad = _adapter()
    ad._init_done.set()
    assert asyncio.run(ad._dispatch("ping", {}, None)) == "pong"


def test_a_hung_init_surfaces_instead_of_hanging_every_caller(monkeypatch):
    monkeypatch.setattr(adapter_mod, "_INIT_WAIT_S", 0.05)
    ad = _adapter()
    ad._init_started = True

    with pytest.raises(RuntimeError, match="still initialising"):
        asyncio.run(ad._dispatch("ping", {}, None))


def test_a_failed_init_gives_the_caller_the_reason():
    ad = _adapter()
    ad._init_started = True

    async def bad_init():
        raise ValueError("no database")

    ad.on_start = bad_init

    async def go():
        with pytest.raises(ValueError):
            await ad._run_init()
        with pytest.raises(RuntimeError, match="no database"):
            await ad._dispatch("ping", {}, None)

    asyncio.run(go())


def test_a_failed_init_propagates_out_of_run(monkeypatch):
    """A service whose init failed must not sit there looking healthy: run()
    returns, the process exits, and the supervisor's respawn budget takes it
    from there."""
    async def bad_init():
        raise ValueError("boom")

    ad = _adapter(on_start=bad_init)

    async def fake_run_target(hub_url, sid, *, name, prefix, overlay):
        await asyncio.sleep(3600)

    monkeypatch.setattr(ad, "_run_target", fake_run_target)
    monkeypatch.setenv("AWM_HUB_URL", "http://127.0.0.1:7819")
    monkeypatch.delenv("AWM_SERVICE_ID", raising=False)

    with pytest.raises(ValueError, match="boom"):
        asyncio.run(ad.run())


def test_no_on_start_opens_the_gate_immediately(monkeypatch):
    ad = _adapter()
    opened: list[bool] = []

    async def fake_run_target(hub_url, sid, *, name, prefix, overlay):
        opened.append(ad._init_done.is_set())

    monkeypatch.setattr(ad, "_run_target", fake_run_target)
    monkeypatch.setenv("AWM_HUB_URL", "http://127.0.0.1:7819")
    monkeypatch.delenv("AWM_SERVICE_ID", raising=False)

    asyncio.run(ad.run())

    assert opened == [True]


def test_an_adapter_never_run_does_not_block_on_a_gate_nobody_opens(monkeypatch):
    """`_dispatch` is a seam tests and embedding harnesses drive directly. With
    no `run()` there is no initialisation pending, so waiting for it would be a
    60s hang for nothing."""
    monkeypatch.setattr(adapter_mod, "_INIT_WAIT_S", 30.0)
    ad = _adapter(on_start=lambda: None)
    assert ad._init_started is False
    assert asyncio.run(ad._dispatch("ping", {}, None)) == "pong"
