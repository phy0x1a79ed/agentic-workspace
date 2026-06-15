"""Seam to the orchestrator's privileged B-ops (contract B).

The orchestrator exposes four privileged operations the agents service calls on
a worker's behalf — ``claim`` / ``deliver`` / ``fail`` / ``decompose_commit``.
They are **manifest-omitted** on the orchestrator side (not MCP tools), so they
are reached through the gateway catch-all dispatch (``/svc/orchestrator/fn/<op>``),
which keys off the orchestrator's ``HANDLERS`` rather than its manifest.

A module-level ``_IMPL`` indirection lets tests and an isolated dev hub inject a
fake orchestrator (``set_impl(FakeOrch())``) so this whole scope builds and
verifies solo. At T3-integration the live default already routes correctly to
the real orchestrator with **zero** change in this service.
"""

from __future__ import annotations

from typing import Any

import awm.gatewayclient as gatewayclient

ORCH_SERVICE = "orchestrator"


class _GatewayOrch:
    """Default impl: each B-op is a gateway call to the orchestrator service."""

    async def claim(self, **kw: Any) -> Any:
        return await gatewayclient.call(ORCH_SERVICE, "claim", kw)

    async def deliver(self, **kw: Any) -> Any:
        return await gatewayclient.call(ORCH_SERVICE, "deliver", kw)

    async def fail(self, **kw: Any) -> Any:
        return await gatewayclient.call(ORCH_SERVICE, "fail", kw)

    async def decompose_commit(self, **kw: Any) -> Any:
        return await gatewayclient.call(ORCH_SERVICE, "decompose_commit", kw)


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


async def claim(**kw: Any) -> Any:
    return await _IMPL.claim(**kw)


async def deliver(**kw: Any) -> Any:
    return await _IMPL.deliver(**kw)


async def fail(**kw: Any) -> Any:
    return await _IMPL.fail(**kw)


async def decompose_commit(**kw: Any) -> Any:
    return await _IMPL.decompose_commit(**kw)
