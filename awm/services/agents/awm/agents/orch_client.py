"""Seam to the orchestrator's privileged B-ops (contract B).

The orchestrator exposes privileged operations the agents service calls on a
placed agent's behalf. They are **manifest-omitted** on the orchestrator side
(not MCP tools), so they are reached through the gateway catch-all dispatch
(``/svc/orchestrator/fn/<op>``), which keys off the orchestrator's ``HANDLERS``
rather than its manifest.

Outcome ops (one per terminal worker action):
    deliver           a worker satisfied an output contract
    fail              a placed agent gave up (or supervision force-failed it)
    decompose_commit  a planner handed back a sub-DAG (DECOMPOSING → children)
    approve_plan      a verifier approved a plan (VERIFYING_PLAN → ACTIVE)
    reject_plan       a verifier rejected a plan (VERIFYING_PLAN → re-plan)

Side-channel ops:
    set_attached      mirror the orthogonal user-attached flag to the kernel so
                      it won't reclaim a task a human is driving
    search_tasks      planner read: existing tasks (so a sub-DAG can reuse nodes)
    search_contracts  planner read: existing contracts

A module-level ``_IMPL`` indirection lets tests and an isolated dev hub inject a
fake orchestrator (``set_impl(FakeOrch())``) so this whole scope builds and
verifies solo. At integration the live default already routes correctly to the
real orchestrator with **zero** change in this service.
"""

from __future__ import annotations

from typing import Any

import awm.gatewayclient as gatewayclient

ORCH_SERVICE = "orchestrator"


class _GatewayOrch:
    """Default impl: each B-op is a gateway call to the orchestrator service."""

    async def deliver(self, **kw: Any) -> Any:
        return await gatewayclient.call(ORCH_SERVICE, "deliver", kw)

    async def fail(self, **kw: Any) -> Any:
        return await gatewayclient.call(ORCH_SERVICE, "fail", kw)

    async def decompose_commit(self, **kw: Any) -> Any:
        return await gatewayclient.call(ORCH_SERVICE, "decompose_commit", kw)

    async def approve_plan(self, **kw: Any) -> Any:
        return await gatewayclient.call(ORCH_SERVICE, "approve_plan", kw)

    async def reject_plan(self, **kw: Any) -> Any:
        return await gatewayclient.call(ORCH_SERVICE, "reject_plan", kw)

    async def set_attached(self, **kw: Any) -> Any:
        return await gatewayclient.call(ORCH_SERVICE, "set_attached", kw)

    async def search_tasks(self, **kw: Any) -> Any:
        return await gatewayclient.call(ORCH_SERVICE, "search_tasks", kw)

    async def search_contracts(self, **kw: Any) -> Any:
        return await gatewayclient.call(ORCH_SERVICE, "search_contracts", kw)


_DEFAULT = _GatewayOrch()
_IMPL: Any = _DEFAULT


def set_impl(impl: Any) -> None:
    """Inject an alternate orchestrator impl (tests / isolated harness)."""
    global _IMPL
    _IMPL = impl


def reset_impl() -> None:
    """Restore the live gateway-routed default."""
    global _IMPL
    _IMPL = _DEFAULT


async def deliver(**kw: Any) -> Any:
    return await _IMPL.deliver(**kw)


async def fail(**kw: Any) -> Any:
    return await _IMPL.fail(**kw)


async def decompose_commit(**kw: Any) -> Any:
    return await _IMPL.decompose_commit(**kw)


async def approve_plan(**kw: Any) -> Any:
    return await _IMPL.approve_plan(**kw)


async def reject_plan(**kw: Any) -> Any:
    return await _IMPL.reject_plan(**kw)


async def set_attached(**kw: Any) -> Any:
    return await _IMPL.set_attached(**kw)


async def search_tasks(**kw: Any) -> Any:
    return await _IMPL.search_tasks(**kw)


async def search_contracts(**kw: Any) -> Any:
    return await _IMPL.search_contracts(**kw)
