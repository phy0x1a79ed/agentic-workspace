"""Live service catalog — the single source of the tools the gateway exposes.

The gateway is the sole interface + coordination hub. Every callable surface it
re-exposes (MCP / CLI / HTTP) is assembled here from two inputs and rendered to
all three surfaces by the (unchanged) ``operations.py`` compiler:

1. **Native ops** — a static table of operations the gateway owns itself
   (``awm_status`` / ``awm_restart`` / ``awm_mcp_sync``). Dispatched to local
   in-process callables.
2. **Registered-service ops** — read live from the hub registry. Each service
   that has opened its control WS and sent a ``ready`` frame carries an ``api``
   manifest on its ``ServiceRecord``; the catalog projects that manifest's
   ``functions`` into tools and dispatches them over the service's control WS
   via ``rpc``.

Because the registry is a process-wide singleton mutated by the WS control
handler, a registration appears in :func:`list_tools` on the next read with no
restart — the MCP stdio proxy re-fetches ``/tools`` on every ``list_tools``,
so tools go live as services come and go.

The registration contract (committed; only the name→tool projection is
implemented this session — full param/CLI/HTTP compilation lands when the first
feature service registers)
--------------------------------------------------------------------------
A service declares its API as a **serializable manifest** in its ``ready``
frame — never by importing a Python class. The hub's registration transport +
lifecycle (``POST /hub/service/register`` → control WS → ``ControlChannel`` →
``call``/``reply`` → supervisor PID journal + 10s reconnect) is reused
unchanged; only the manifest is extended. Each ``functions[]`` entry is the
serializable shape of an ``operations.Operation`` (minus the Python callable)::

    { "name", "description",
      "params": [{name, type, required, default, description}],
      "surfaces": ["mcp", "cli", "http"],
      "http": {"method", "path"}?,          # optional; gateway derives a default
      "cli":  {"group", "command"}?,         # optional; gateway derives a default
      "no_response": false }

plus ``emitters: [{topic, transport}]`` (events the service publishes) and
``subscriptions: [{service, topic}]`` (events it consumes).

Threading into the API-generation layer: the catalog builds an ``Operation``
per function whose ``service_func`` is a closure that does
``rpc.get_control(sid).call(fn, args, as_=caller)``; from there ``operations.py``
generates the MCP schema / HTTP route / CLI command without caring whether the
callable is in-process or an RPC closure. Dispatch is **catalog-owned** (this
module) — ``operations.py`` is used only to *describe* surfaces.

Hub-mediated comms (direction only; not built this session): services reach
each other only through the gateway, never via direct sockets —
``call`` (request/reply, reusing ``ControlChannel.call``), ``emit``/``sub``
(pub/sub: a service subscribes to another's topic exactly as browsers do today,
and the hub fans emitted events to service subscribers), and the ``Bridge``
relay for service↔service streaming. Every path carries the ``as_`` identity.
This is the "validate refs via gateway RPC, cached" mechanism and keeps the
gateway the sole router.

Concurrency: the uvicorn/FastAPI server loop owns all async hub state
(ControlChannels, WS coroutines, lease holds). :func:`dispatch` is ``async``;
native ops (sync, potentially blocking) are offloaded via ``run_in_threadpool``
and service ops are awaited directly on the loop. ``asyncio.run()`` is never
used in-process. :func:`list_tools` stays sync over a GIL-safe registry
snapshot.
"""

from __future__ import annotations

import inspect
import json
import logging
import time
from typing import Any

from mcp.types import Tool
from starlette.concurrency import run_in_threadpool

from awm.gateway.gateway_ops import GATEWAY_OPERATIONS
from awm.gateway.hub import rpc
from awm.gateway.hub.registry import ServiceRecord, get_registry
from awm.gateway.operations import _call_service, operations_to_mcp_tools

log = logging.getLogger("awm.gateway.catalog")


# ---------------------------------------------------------------------------
# Core uptime tracking (moved here from the deleted tool_dispatch.py)
# ---------------------------------------------------------------------------

_CORE_START: float | None = None


def mark_core_start() -> None:
    """Record the core process start time for uptime reporting in awm_status."""
    global _CORE_START
    _CORE_START = time.time()


def core_uptime() -> int | None:
    """Seconds since :func:`mark_core_start`, or ``None`` if never marked.
    Read by the ``awm_status`` Operation handler in ``gateway_ops``."""
    return int(time.time() - _CORE_START) if _CORE_START else None


def _serialize(obj: Any) -> str:
    """Render a dispatch result as the string ``/invoke`` returns and the MCP
    proxy hands straight to ``TextContent``."""
    if isinstance(obj, str):
        return obj
    return json.dumps(obj, indent=2, default=str)


# ---------------------------------------------------------------------------
# Gateway control ops — generated from the declarative GATEWAY_OPERATIONS
# registry (the gateway's own control plane: status / restart / mcp-sync /
# hub list+deregister / services lifecycle). Their MCP tools + HTTP routes +
# CLI commands all come from one Operation each — see gateway_ops.py.
# ---------------------------------------------------------------------------

# MCP-surface gateway tools, keyed by name for dispatch + uniqueness reservation.
_GATEWAY_MCP_TOOLS: list[Tool] = operations_to_mcp_tools(GATEWAY_OPERATIONS)
_GATEWAY_OPS_BY_NAME = {
    op.name: op for op in GATEWAY_OPERATIONS if "mcp" in op.surfaces
}


# ---------------------------------------------------------------------------
# Registered-service ops — projected live from the hub registry
# ---------------------------------------------------------------------------


def _tool_name(rec: ServiceRecord, fn: dict) -> str:
    """MCP tool name for a service function.

    A manifest function may carry an explicit ``"tool"`` key to choose its
    exact MCP-surface name — this decouples the projected tool label from the
    internal op ``name`` used for service↔service RPC dispatch, so the frozen
    ``IDENTITY_CONTRACT.md`` names (``resolveScope`` …) keep dispatching while
    the surface reads cleanly (``scope_resolve``). With no override the name
    falls back to ``{service}_{fn}``.

    Overrides drop the automatic global-uniqueness the ``{service}_{fn}`` form
    gave us (service names are unique); :func:`list_tools` enforces uniqueness
    by warn-and-skip, so override names MUST be globally unique."""
    return fn.get("tool") or f"{rec.name}_{fn['name']}"


def _fn_to_tool(rec: ServiceRecord, fn: dict) -> Tool:
    """Project one manifest ``functions[]`` entry into an MCP Tool schema."""
    props: dict[str, Any] = {}
    required: list[str] = []
    for p in fn.get("params", []) or []:
        prop: dict[str, Any] = {"type": p.get("type", "string")}
        if p.get("description"):
            prop["description"] = p["description"]
        if p.get("default") is not None:
            prop["default"] = p["default"]
        if prop["type"] == "array":
            prop["items"] = {"type": "string"}
        props[p["name"]] = prop
        if p.get("required"):
            required.append(p["name"])
    schema: dict[str, Any] = {"type": "object", "properties": props}
    if required:
        schema["required"] = required
    return Tool(
        name=_tool_name(rec, fn),
        description=fn.get("description", ""),
        inputSchema=schema,
    )


def list_tools() -> list[Tool]:
    """Native tools + every registered service's declared functions. Sync over a
    GIL-safe registry snapshot — never awaits, never blocks.

    Projected names must be globally unique. The ``{service}_{fn}`` fallback is
    collision-free (service names are unique), but explicit ``"tool"`` overrides
    are not — so we warn-and-skip duplicates (first registrant wins) rather than
    raise: a raised error here would 500 ``/tools`` and blind every MCP client,
    which re-fetches it constantly. Gateway control-op names are reserved up
    front (generated from GATEWAY_OPERATIONS, not hand-rolled)."""
    tools: list[Tool] = list(_GATEWAY_MCP_TOOLS)
    seen: set[str] = {t.name for t in _GATEWAY_MCP_TOOLS}
    for rec in get_registry().service_records():
        for fn in (rec.api or {}).get("functions", []) or []:
            if not (isinstance(fn, dict) and fn.get("name")):
                continue
            tool = _fn_to_tool(rec, fn)
            if tool.name in seen:
                log.warning(
                    "duplicate MCP tool name %r from service %r (fn %r) — skipping",
                    tool.name, rec.name, fn["name"],
                )
                continue
            seen.add(tool.name)
            tools.append(tool)
    return tools


def _find_service_fn(name: str) -> tuple[ServiceRecord | None, str | None]:
    for rec in get_registry().service_records():
        for fn in (rec.api or {}).get("functions", []) or []:
            if isinstance(fn, dict) and fn.get("name") and _tool_name(rec, fn) == name:
                return rec, fn["name"]
    return None, None


async def dispatch(name: str, args: dict, as_: str | None = None) -> str:
    """Route a tool call to its handler and return the serialized result.

    Gateway control ops dispatch through their ``Operation`` (the same handler
    that backs the HTTP route + CLI command): async ones are awaited on the
    server loop, sync ones run in a threadpool. Service ops are awaited over the
    service's control WS. See the module concurrency note. Raises ``ValueError``
    for an unknown tool (→ 404) and ``RuntimeError`` when a service's control
    channel is not open (→ 500), matching ``/invoke``'s existing exception→HTTP
    translation.
    """
    op = _GATEWAY_OPS_BY_NAME.get(name)
    if op is not None:
        if inspect.iscoroutinefunction(op.service_func):
            return _serialize(await _call_service(op, args))
        return _serialize(await run_in_threadpool(_call_service, op, args))

    rec, fn = _find_service_fn(name)
    if rec is None or fn is None:
        raise ValueError(f"Unknown tool: {name}")
    ch = rpc.get_control(rec.service_id)
    if ch is None:
        raise RuntimeError(f"service {rec.name!r} control channel not open")
    return _serialize(await ch.call(fn, args, as_=as_))
