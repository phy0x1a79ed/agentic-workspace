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
from awm.gateway.operations import _call_service, _to_mcp_tool, operations_to_mcp_tools

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


# ---------------------------------------------------------------------------
# Per-domain projection (the collapsed MCP read surface) — T1
# ---------------------------------------------------------------------------
# The expanded ``list_tools()`` projection above stays exactly as-is (the CLI
# generator and the flat ``/invoke`` dispatch both depend on it). This second,
# *parallel* projection folds that same surface by **domain** — the projected
# tool name split on the first underscore (``scope_create`` → domain ``scope`` /
# verb ``create``). One service can yield several domains (scopes → scope /
# project / ref); gateway-native ops group by their ``cli_group`` (``gateway`` /
# ``services``) with the verb being their ``cli_command``. The MCP stdio proxy
# requests this view (``GET /tools?view=domains``), so a non-deferring client
# carries ~8–10 generic tools instead of ~71 verb-tools; verbs + their full
# param schemas are learned on demand via the reserved ``describe`` verb.

_DESCRIBE_VERB = "describe"

# The minimal envelope every domain tool advertises (discovery-only — the rich
# per-verb schema is fetched via ``describe``, never inlined here).
_DOMAIN_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "verb": {
            "type": "string",
            "description": "The operation to run within this domain. Use "
                           "verb='describe' (optionally args={verb:<name>}) to "
                           "list the domain's verbs and their parameter schemas.",
        },
        "args": {
            "type": "object",
            "description": "Arguments for the chosen verb (see describe).",
        },
    },
    "required": ["verb"],
}


def _domain_catalog() -> dict[str, list[dict[str, Any]]]:
    """Fold the live (native + service) tool surface into ``{domain: [verb…]}``.

    Each verb entry is ``{"verb": str, "tool": Tool}`` — the ``Tool`` carries the
    full per-verb description + ``inputSchema`` (byte-identical to the expanded
    projection), reused verbatim by :func:`_describe_domain`. Native ops are
    folded first so a service can never shadow a gateway-native verb (first-wins,
    mirroring ``list_tools``'s duplicate handling). Sync over a GIL-safe registry
    snapshot — same contract as ``list_tools``."""
    domains: dict[str, list[dict[str, Any]]] = {}
    seen: set[tuple[str, str]] = set()

    def _add(domain: str, verb: str, tool: Tool, *, origin: str) -> None:
        key = (domain, verb)
        if key in seen:
            log.warning(
                "duplicate domain verb %s/%s from %s — skipping", domain, verb, origin)
            return
        seen.add(key)
        domains.setdefault(domain, []).append({"verb": verb, "tool": tool})

    # Native gateway control ops: domain = cli_group, verb = cli_command.
    for op in GATEWAY_OPERATIONS:
        if "mcp" not in op.surfaces:
            continue
        _add(op.cli_group, op.cli_command, _to_mcp_tool(op), origin="gateway-native")

    # Registered services: domain/verb from the projected tool name.
    for rec in get_registry().service_records():
        for fn in (rec.api or {}).get("functions", []) or []:
            if not (isinstance(fn, dict) and fn.get("name")):
                continue
            tname = _tool_name(rec, fn)
            domain, _, verb = tname.partition("_")
            if not verb:  # no underscore → single-verb domain named after itself
                verb = domain
            _add(domain, verb, _fn_to_tool(rec, fn), origin=f"service {rec.name!r}")

    return domains


def list_domain_tools() -> list[Tool]:
    """Project the collapsed per-domain MCP surface — one ``Tool`` per domain,
    each advertising the minimal ``{verb, args}`` envelope. Names every verb in
    the free-text description (cheap discovery) while keeping the schema minimal
    per the discovery-only decision."""
    out: list[Tool] = []
    for domain, verbs in _domain_catalog().items():
        verb_names = [v["verb"] for v in verbs]
        desc = (
            f"Generic '{domain}' domain tool. Verbs: {', '.join(verb_names)}. "
            f"Call with {{verb, args}}; verb='describe' (optionally "
            f"args={{verb:<name>}}) returns full parameter schemas."
        )
        out.append(Tool(name=domain, description=desc,
                        inputSchema=dict(_DOMAIN_INPUT_SCHEMA)))
    return out


def _describe_domain(domain: str, verb: str | None = None,
                     catalog: dict[str, list[dict[str, Any]]] | None = None) -> dict:
    """Answer ``verb='describe'`` from the catalog alone (no service round-trip).

    Returns ``{domain, verbs:[{verb, description, params}]}`` where ``params`` is
    the verb's full ``inputSchema`` — the same schema the expanded per-verb tool
    advertised. ``verb`` narrows to a single entry."""
    cat = catalog if catalog is not None else _domain_catalog()
    verbs = cat.get(domain)
    if verbs is None:
        raise ValueError(f"Unknown domain: {domain}")
    items = [v for v in verbs if verb is None or v["verb"] == verb]
    if verb is not None and not items:
        raise ValueError(f"Unknown verb {verb!r} for domain {domain!r}")
    return {
        "domain": domain,
        "verbs": [
            {"verb": v["verb"], "description": v["tool"].description,
             "params": v["tool"].inputSchema}
            for v in items
        ],
    }


def _find_native_op(domain: str, verb: str):
    """Locate the MCP-surface gateway-native Operation for a domain/verb pair
    (domain = ``cli_group``, verb = ``cli_command``), else ``None``."""
    for op in GATEWAY_OPERATIONS:
        if "mcp" in op.surfaces and op.cli_group == domain and op.cli_command == verb:
            return op
    return None


async def _dispatch_domain(name: str, args: dict, as_: str | None,
                           catalog: dict[str, list[dict[str, Any]]]) -> str:
    """Dispatch a collapsed per-domain tool call (``{verb, args}``).

    ``verb='describe'`` is answered from the catalog. A native verb routes
    through its Operation (same handler as the HTTP route + CLI command); a
    service verb is resolved back to its internal function via the existing
    ``_find_service_fn`` reverse lookup (so a name≠tool divergence like
    ``scope_refresh`` → internal ``awm_refresh`` still routes) and RPC'd with the
    ``as_`` placement identity threaded exactly as the flat path does."""
    verb = args.get("verb")
    inner = args.get("args") or {}
    if not isinstance(inner, dict):
        raise ValueError("'args' must be an object")
    if verb == _DESCRIBE_VERB:
        return _serialize(_describe_domain(name, inner.get("verb"), catalog))

    op = _find_native_op(name, verb)
    if op is not None:
        if inspect.iscoroutinefunction(op.service_func):
            return _serialize(await _call_service(op, inner))
        return _serialize(await run_in_threadpool(_call_service, op, inner))

    rec, fn = _find_service_fn(f"{name}_{verb}")
    if rec is None and verb == name:  # single-verb (no-underscore) domain
        rec, fn = _find_service_fn(name)
    if rec is None or fn is None:
        raise ValueError(f"Unknown verb {verb!r} for domain {name!r}")
    ch = rpc.get_control(rec.service_id)
    if ch is None:
        raise RuntimeError(f"service {rec.name!r} control channel not open")
    return _serialize(await ch.call(fn, inner, as_=as_))


async def dispatch(name: str, args: dict, as_: str | None = None) -> str:
    """Route a tool call to its handler and return the serialized result.

    Gateway control ops dispatch through their ``Operation`` (the same handler
    that backs the HTTP route + CLI command): async ones are awaited on the
    server loop, sync ones run in a threadpool. Service ops are awaited over the
    service's control WS. See the module concurrency note. Raises ``ValueError``
    for an unknown tool (→ 404) and ``RuntimeError`` when a service's control
    channel is not open (→ 500), matching ``/invoke``'s existing exception→HTTP
    translation.

    Collapsed per-domain calls (the ``GET /tools?view=domains`` surface) arrive
    here too: a payload whose ``name`` is a known domain and whose ``args``
    carries a ``verb`` is routed to :func:`_dispatch_domain` *ahead* of the flat
    branches. The ``"verb" in args`` guard keeps a future flat tool literally
    named like a domain from being hijacked, and the flat branches below stay
    intact so the CLI's by-name ``/invoke`` posts keep working.
    """
    args = args or {}
    if "verb" in args:
        domains = _domain_catalog()
        if name in domains:
            return await _dispatch_domain(name, args, as_, domains)

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
