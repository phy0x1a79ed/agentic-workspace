"""Seam to the orchestrator's privileged B-ops (contract B).

The orchestrator exposes privileged operations the agents service calls on a
placed agent's behalf. They are **manifest-omitted** on the orchestrator side
(not MCP tools), so they are reached through the gateway catch-all dispatch
(``/svc/orchestrator/fn/<op>``), which keys off the orchestrator's ``HANDLERS``
rather than its manifest.

The seam shrank with the lift (T4a): the worker VERDICTS (approve_plan /
reject_plan / accept_work / reject_work), the planner READS (search_tasks /
search_contracts), and the generic admin relay (``call_op``) are no longer
called from here — a placed agent drives those directly through the
orchestrator's own ``orchwork`` MCP domain. What the agents service still calls
on a placement's behalf is the SUPERVISION + side-channel surface:

Supervision outcomes (driven by the agents-side supervisor, not the worker):
    deliver           a worker's staged deliverable was accepted at the stop
                      boundary → satisfy an output contract
    fail              supervision force-failed a placement (stall / budget /
                      reclaim of a dead session)
    decompose_commit  a planner's buffered sub-DAG was accepted (DECOMPOSING →
                      children)

Side-channel ops (mirror the agents-owned steering state to the kernel):
    set_attached      mirror the orthogonal user-attached flag so the kernel
                      won't reclaim a task a human is driving
    set_steering      mirror the durable steering state (attach + the three
                      consent/steering bits + the objective record); the agents
                      side is the single writer, the orchestrator only mirrors
    set_paused        mirror the sticky human-pause flag to the kernel (durable;
                      keeps the supervisor frozen even after the human detaches)

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

    async def set_attached(self, **kw: Any) -> Any:
        return await gatewayclient.call(ORCH_SERVICE, "set_attached", kw)

    async def set_steering(self, **kw: Any) -> Any:
        return await gatewayclient.call(ORCH_SERVICE, "set_steering", kw)

    async def set_paused(self, **kw: Any) -> Any:
        return await gatewayclient.call(ORCH_SERVICE, "orch_set_paused", kw)


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


async def set_attached(**kw: Any) -> Any:
    return await _IMPL.set_attached(**kw)


async def set_steering(**kw: Any) -> Any:
    return await _IMPL.set_steering(**kw)


async def set_paused(**kw: Any) -> Any:
    return await _IMPL.set_paused(**kw)
