"""until_provided waits while no plugin provides a contract, and stops waiting when one does."""

import asyncio

import httpx

import awm.gatewayclient as gc


async def test_it_waits_until_a_provider_appears(monkeypatch):
    answers = iter([False, False, True])
    sleeps = []

    async def provided(contract, major):
        assert (contract, major) == ("operations", 2)
        return next(answers)

    async def sleep(s):
        sleeps.append(s)

    monkeypatch.setattr(gc, "provided", provided)
    monkeypatch.setattr(asyncio, "sleep", sleep)
    await gc.until_provided("operations", 2, idle_s=7)
    assert sleeps == [7, 7]


async def test_a_gateway_that_cannot_say_does_not_hold_the_caller(monkeypatch):
    async def provided(contract, major):
        raise httpx.ConnectError("gateway down")

    monkeypatch.setattr(gc, "provided", provided)
    await asyncio.wait_for(gc.until_provided("operations", 2), timeout=1)
