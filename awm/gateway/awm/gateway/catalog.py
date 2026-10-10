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

import copy
import inspect
import json
import logging
import time
from typing import Any

from mcp.types import Tool
from starlette.concurrency import run_in_threadpool

from awm.config import caller_peer, peer_record, verb_category, verb_effect
from awm.gateway import mcp_more, peer_catalog
from awm.gateway.gateway_ops import GATEWAY_OPERATIONS, bound_caller
from awm.gateway.hub import discovery, rpc
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


def _fn_on_surface(fn: dict, surface: str) -> bool:
    """Whether a manifest function is projected onto ``surface`` ("mcp"/"cli"/"http").

    Honors the documented per-function ``"surfaces"`` field (see the module
    docstring's registration contract). A function that omits ``surfaces`` (as
    every current service does) defaults to all three, so this is a pure no-op
    for the existing tree; a function declaring e.g. ``["cli", "http"]`` is kept
    off the MCP surface — the mechanism behind CLI-only write verbs. Only the
    per-domain MCP projection consults this; ``list_tools()`` stays unfiltered so
    the CLI generator and flat ``/invoke`` by-name dispatch keep every verb."""
    surfaces = fn.get("surfaces")
    if not surfaces:
        return True
    return surface in surfaces


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


def list_tools(grants: frozenset[str] | None = None) -> list[Tool]:
    """Native tools + every registered service's declared functions. Sync over a
    GIL-safe registry snapshot — never awaits, never blocks.

    Projected names must be globally unique. The ``{service}_{fn}`` fallback is
    collision-free (service names are unique), but explicit ``"tool"`` overrides
    are not — so we warn-and-skip duplicates (first registrant wins) rather than
    raise: a raised error here would 500 ``/tools`` and blind every MCP client,
    which re-fetches it constantly. Gateway control-op names are reserved up
    front (generated from GATEWAY_OPERATIONS, not hand-rolled).

    ``grants`` is a foreign caller's read grants (see :func:`foreign_grants`);
    with it, only the tools that caller may call are listed."""
    if grants is not None:
        entries = _flat_entries()
        return [t for t in list_tools()
                if _allowed(entries.get(t.name, {}).get("effect"),
                            entries.get(t.name, {}).get("category"), grants)]
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
# Foreign-caller gate
# ---------------------------------------------------------------------------
# A caller the edge stamped ``peer:<node>`` where the peer book does not call
# <node> domestic is foreign. It may call a verb only when the verb declares
# ``effect = read`` AND a ``category`` the peer's record grants. Everything else
# is refused with the error an unknown tool gets, so a refusal reveals nothing
# about what exists. The peer book is read on every call: a revoked grant bites
# on the next one. The bare legacy ``peer`` and an absent identity are not
# stamped by the edge as a node, so they are untouched.


def foreign_grants(as_: str | None) -> frozenset[str] | None:
    """The read categories a foreign caller holds, or ``None`` if the caller is
    not foreign (a domestic node, the legacy bare ``peer``, or no identity).

    A node missing from the peer book is foreign with no grants, and so is any
    stamp that starts with ``peer:`` but names no node (``peer:``, ``peer: ``):
    a malformed stamp fails closed."""
    node = caller_peer(as_)
    if node is None:
        return frozenset() if isinstance(as_, str) and as_.startswith("peer:") else None
    record = peer_record(node)
    if record is None:
        return frozenset()
    if record["relation"] == "domestic":
        return None
    return frozenset(g for g in record["grants"] if isinstance(g, str))


def _fn_policy(fn: dict) -> tuple[str, str | None]:
    """``(effect, category)`` a manifest function declares. An effect the book
    of effects does not know counts as ``write``: a typo must not open a verb."""
    try:
        effect = verb_effect(fn)
    except ValueError:
        log.warning("function %r declares an unknown effect; treating it as write",
                    fn.get("name"))
        effect = "write"
    category = verb_category(fn)
    return effect, category if isinstance(category, str) else None


def _allowed(effect: Any, category: Any, grants: frozenset[str]) -> bool:
    """Whether a verb declaring ``effect``/``category`` is open to a caller holding
    ``grants``. Anything but strings is refused, so one malformed manifest entry
    cannot raise inside the gate."""
    return (isinstance(effect, str) and effect == "read"
            and isinstance(category, str) and category in grants)


def _flat_entries() -> dict[str, dict[str, Any]]:
    """``{flat tool name: entry}`` with the precedence ``dispatch`` and
    :func:`list_tools` use: native ops first, then the first registrant.

    An entry is ``{effect, category, op, rec, fn}`` where exactly one of ``op``
    and ``rec``/``fn`` is set: the thing the gate approves is the thing that
    runs. A native op never carries a category: its handler runs in-process
    without the caller's identity, so no grant can reach it."""
    out: dict[str, dict[str, Any]] = {
        name: {"effect": op.effect, "category": None, "op": op, "rec": None, "fn": None}
        for name, op in _GATEWAY_OPS_BY_NAME.items()}
    for rec in get_registry().service_records():
        for fn in (rec.api or {}).get("functions", []) or []:
            if isinstance(fn, dict) and fn.get("name"):
                effect, category = _fn_policy(fn)
                out.setdefault(_tool_name(rec, fn), {
                    "effect": effect, "category": category,
                    "op": None, "rec": rec, "fn": fn["name"]})
    return out


def _foreign_catalog(catalog: dict[str, list[dict[str, Any]]],
                     grants: frozenset[str]) -> dict[str, list[dict[str, Any]]]:
    """``catalog`` cut to the verbs ``grants`` allow; a domain with none is gone."""
    out: dict[str, list[dict[str, Any]]] = {}
    for domain, verbs in catalog.items():
        kept = [v for v in verbs if _allowed(v["effect"], v["category"], grants)]
        if kept:
            out[domain] = kept
    return out


def _has_peer_arg(args: dict) -> bool:
    """Whether a call asks to be routed to a node, in the envelope, as a flat
    argument or inside the verb's own arguments. A foreign caller gets no onward
    hops."""
    inner = args.get("args")
    return args.get("peer") is not None or (
        isinstance(inner, dict) and inner.get("peer") is not None)


def _fn_timeout(rec: ServiceRecord, internal_name: str) -> float | None:
    """Per-function RPC timeout declared in the manifest, else ``None`` (default).

    Honors the documented per-function ``"timeout"`` field on the ``/invoke``
    dispatch path (the ``/svc/<name>/fn/<fn>`` proxy already honors it) so a
    slow handler — a bulk re-embed / dedup pass — isn't capped at the 30s
    ``ControlChannel.call`` default when invoked via the catalog."""
    for fn in (rec.api or {}).get("functions", []) or []:
        if isinstance(fn, dict) and fn.get("name") == internal_name and fn.get("timeout"):
            return float(fn["timeout"])
    return None


async def _rpc_call(rec: ServiceRecord, fn: str, args: dict, as_: str | None) -> Any:
    """RPC a service function over its control WS, applying its manifest timeout."""
    ch = rpc.get_control(rec.service_id)
    if ch is None:
        raise RuntimeError(f"service {rec.name!r} control channel not open")
    timeout = _fn_timeout(rec, fn)
    if timeout is not None:
        return await ch.call(fn, args, as_=as_, timeout=timeout)
    return await ch.call(fn, args, as_=as_)


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
# requests this view, so a non-deferring client carries a few dozen generic tools
# instead of hundreds of verb-tools; verbs + their full param schemas are learned
# on demand via the reserved ``describe`` verb.
#
# ``?view=domains&peers=1`` widens the same projection to the **fleet**: still one
# tool per domain name, but a domain only a peer runs appears too, and the
# envelope's ``peer`` key chooses the node. That replaced merging each peer's whole
# catalog under ``<domain>@<peer>`` names, which multiplied the tool count by the
# number of peers joined while adding no capability at all. Which node a call lands
# on by default is ``peer_catalog``'s decision, not this module's.

_DESCRIBE_VERB = "describe"

#: The reserved top-level tool that reports where a domain can run. Not a domain
#: verb: folding it into the domain scheme would yield the junk single-verb shape
#: ``providersOf(verb="providersOf")``, so it is emitted directly and dispatched
#: ahead of the domain branch — the federation analogue of the reserved
#: ``describe`` verb.
_PROVIDERS_TOOL = "providersOf"

#: The reserved call-through tool for discoverable domains (see ``mcp_more``).
_MORE_TOOL = mcp_more.MORE_TOOL

#: Gateway-native domains that are core. They have no service folder to carry a
#: ``tier`` key, so they are named here; ``gateway`` and ``config`` stay
#: discoverable.
_NATIVE_CORE_DOMAINS = frozenset({"services", "peer"})

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
        "peer": {
            "type": "string",
            "description": "Optional. Run this verb on a specific fleet node "
                           "instead of the tool's default provider (usually this "
                           "node; a singleton's owner for a singleton). "
                           "providersOf(tool=<domain>) lists the valid values "
                           "and which one is the default.",
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
    mirroring ``list_tools``'s duplicate handling). Each entry also carries the
    verb's declared ``effect`` and ``category`` (which the foreign-caller gate
    reads) and the ``op`` or ``rec``/``fn`` it resolves to, so dispatch runs
    exactly the entry that was approved. A native op never has a category. Sync over a GIL-safe registry snapshot — same contract as
    ``list_tools``."""
    domains: dict[str, list[dict[str, Any]]] = {}
    seen: set[tuple[str, str]] = set()

    def _add(domain: str, verb: str, tool: Tool, *, origin: str,
             effect: str, category: str | None, op: Any = None,
             rec: ServiceRecord | None = None, fn: str | None = None) -> None:
        key = (domain, verb)
        if key in seen:
            log.warning(
                "duplicate domain verb %s/%s from %s — skipping", domain, verb, origin)
            return
        seen.add(key)
        domains.setdefault(domain, []).append(
            {"verb": verb, "tool": tool, "effect": effect, "category": category,
             "op": op, "rec": rec, "fn": fn})

    # Native gateway control ops: domain = cli_group, verb = cli_command.
    for op in GATEWAY_OPERATIONS:
        if "mcp" not in op.surfaces:
            continue
        _add(op.cli_group, op.cli_command, _to_mcp_tool(op), origin="gateway-native",
             effect=op.effect, category=None, op=op)

    # Registered services: domain/verb from the projected tool name.
    for rec in get_registry().service_records():
        for fn in (rec.api or {}).get("functions", []) or []:
            if not (isinstance(fn, dict) and fn.get("name")):
                continue
            if not _fn_on_surface(fn, "mcp"):
                continue  # CLI/HTTP-only verb — kept off the agent-facing MCP surface
            tname = _tool_name(rec, fn)
            domain, _, verb = tname.partition("_")
            if not verb:  # no underscore → single-verb domain named after itself
                verb = domain
            effect, category = _fn_policy(fn)
            _add(domain, verb, _fn_to_tool(rec, fn), origin=f"service {rec.name!r}",
                 effect=effect, category=category, rec=rec, fn=fn["name"])

    return domains


def _domain_blurbs() -> dict[str, str]:
    """``{domain: prose}`` from each service's optional manifest ``description``.

    Everything else the surface shows about a domain is generated — the name and
    the verb list — so a service had no way to say how it is meant to be USED,
    only what it can do. That gap is not cosmetic: an agent reading `2fa` sees
    ten capable-looking verbs and no hint that `ssh(verb=connect)` already arms
    the approver for it, and calling them by hand spends Duo budget the fleet
    arbiter is not counting.

    The key is additive. A manifest without it is unchanged, and the gateway
    reads no other top-level manifest key for presentation.

    A service projecting several domains (scopes → scope / project / ref) gets
    its blurb on every one of them. That is deterministic where picking one
    would be arbitrary — and a service whose domains want different prose should
    say it per verb, which is what ``description`` on a function is for. A
    domain with no MCP-visible verb gets nothing, since it has no tool to
    describe.
    """
    out: dict[str, str] = {}
    for rec in get_registry().service_records():
        api = rec.api or {}
        blurb = api.get("description")
        if not isinstance(blurb, str) or not blurb.strip():
            continue
        for fn in api.get("functions", []) or []:
            if not (isinstance(fn, dict) and fn.get("name")):
                continue
            if not _fn_on_surface(fn, "mcp"):
                continue
            out.setdefault(_tool_name(rec, fn).partition("_")[0], blurb.strip())
    return out


def _domain_envelope(verb_names: list[str], *, with_peer: bool = True) -> dict[str, Any]:
    """The ``{verb, args, peer}`` envelope schema, with ``verb`` enumerated.

    Deep-copied — a shallow copy shares the nested ``properties`` dict, so
    stamping the enum would mutate the module constant for every later domain.

    The enum is discovery, not enforcement: dispatch validates a verb against the
    live catalog (locally) or lets the target peer validate its own, so a snapshot
    that is a couple of minutes stale can never block a call that would work.
    """
    schema = copy.deepcopy(_DOMAIN_INPUT_SCHEMA)
    if not with_peer:
        del schema["properties"]["peer"]
    if verb_names:
        schema["properties"]["verb"]["enum"] = [*verb_names, _DESCRIBE_VERB]
    return schema


def _local_domain_verbs(
        catalog: dict[str, list[dict[str, Any]]]) -> dict[str, list[str]]:
    """``{domain: [verb…]}`` for this node — the shape ``peer_catalog`` reasons
    over, so it never has to import this module back."""
    return {domain: [v["verb"] for v in verbs] for domain, verbs in catalog.items()}


def _providers_tool() -> Tool:
    """The reserved top-level ``providersOf`` tool (see ``_PROVIDERS_TOOL``)."""
    return Tool(
        name=_PROVIDERS_TOOL,
        description=(
            "Which fleet nodes can serve a given awm MCP tool, and which one it "
            "goes to by default. Pass the tool/domain name, e.g. "
            "providersOf(tool='2fa'). Use it before setting peer= on a domain "
            "call: an ordinary domain defaults to this node, a singleton has "
            "exactly one valid provider, and a domain no local service provides "
            "may require peer= explicitly."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "tool": {"type": "string",
                         "description": "Domain/tool name to look up. Omit for "
                                        "the whole fleet map."},
                "refresh": {"type": "boolean",
                            "description": "Force a fresh sweep of every peer "
                                           "instead of reading the cached one.",
                            "default": False},
            },
        },
    )


async def _providers_of(tool: str | None = None,
                        refresh: bool = False) -> dict[str, Any]:
    """Answer ``providersOf`` — where a tool can run, and where it goes by default.

    Shared by the reserved MCP tool and the ``peer providers`` Operation (so
    ``awm peer providers`` and the HTTP route can never drift from what the agent
    sees). ``refresh`` forces a synchronous peer sweep; without it this reads the
    background snapshot and never touches the network. Omit ``tool`` for the whole
    fleet map.

    An unreachable peer is reported as unreachable with its last-known domains,
    never dropped: "mira does not have this" and "mira is asleep" are different
    answers and conflating them is what makes a discovery tool useless.
    """
    if refresh:
        await peer_catalog.sweep()
    snap = peer_catalog.snapshot()
    local = _local_domain_verbs(_domain_catalog())
    swept = peer_catalog.swept_at()
    if tool:
        return {**peer_catalog.resolve(tool, local, snap), "swept_at": swept}
    return {
        "swept_at": swept,
        "tools": [peer_catalog.resolve(d, local, snap)
                  for d in peer_catalog.fleet_domains(local, snap)],
    }


#: Domains an UNREGISTERED service folder provides, where they differ from the
#: folder name. A registered service names its own domains in its manifest; this
#: table is only the fallback for a folder whose service is down, profile-gated
#: off, or run only by a peer. A new core folder whose domains differ from its
#: name must be added here, or its domain stays discoverable while it is down.
_FOLDER_DOMAINS: dict[str, tuple[str, ...]] = {
    "scopes": ("scope", "project", "ref"),
    "rlm-browser": ("rlm",),
}


def _core_domains() -> set[str]:
    """Domains this node declares core.

    Read from the ``service.toml`` files on disk, not from what is running, so a
    core domain stays listed when its local service is down, gated off by profile
    or served only by a peer. Per core folder: the domains its registered service
    projects, else the fallback in ``_FOLDER_DOMAINS`` (default: the folder name).
    Plus the gateway-native core set. A domain no local folder declares core is
    never in this set, whatever a peer calls it. A core domain nothing serves
    anywhere is in the set but never reaches a listing, which walks the fleet's
    domains."""
    core = set(_NATIVE_CORE_DOMAINS)
    registered: dict[str, list[ServiceRecord]] = {}
    for rec in get_registry().service_records():
        registered.setdefault(rec.name, []).append(rec)
    for folder in discovery.core_services():
        recs = registered.get(folder)
        if not recs:
            core.update(_FOLDER_DOMAINS.get(folder, (folder,)))
            continue
        for rec in recs:
            for fn in (rec.api or {}).get("functions", []) or []:
                if not (isinstance(fn, dict) and fn.get("name")):
                    continue
                if not _fn_on_surface(fn, "mcp"):
                    continue
                core.add(_tool_name(rec, fn).partition("_")[0])
    return core


def _one_line(text: str, limit: int = 110) -> str:
    """First sentence of ``text`` on one line, capped at ``limit`` characters."""
    flat = " ".join(text.split())
    head, sep, _ = flat.partition(". ")
    flat = head + ("." if sep else "")
    return flat if len(flat) <= limit else flat[: limit - 1].rstrip() + "…"


def _node_phrase(res: dict[str, Any]) -> str:
    """Where a domain runs by default, in a few words."""
    reason = res["reason"]
    others = [p["peer"] for p in res["providers"] if p["peer"] != res["default"]]
    if reason == peer_catalog.REASON_LOCAL:
        return "this node" + (f", also {', '.join(others)}" if others else "")
    if reason == peer_catalog.REASON_SINGLETON:
        return f"{res['default']} (singleton)"
    if reason == peer_catalog.REASON_SOLE_PEER:
        return str(res["default"])
    if reason == peer_catalog.REASON_AMBIGUOUS:
        return "peer= required: " + ", ".join(p["peer"] for p in res["providers"])
    return "unknown"


#: Ceiling on the ``more`` tool description, which every session pays for in
#: full. The complete index is always one ``more()`` call away.
_MORE_DESCRIPTION_CAP = 1500
_MORE_OVERFLOW = "… call more() with no domain for the full list"
_COMPACT_VERBS = 5
_COMPACT_BLURB = 80


def _discoverable_line(res: dict[str, Any], blurb: str | None, *,
                       compact: bool = False) -> str:
    """One index line. Full form: name, the service's own blurb (else all its verb
    names), node. Compact form, for the tool description: name plus the blurb or
    at most five verbs, and the node only when it is not this one."""
    verbs = _advertised_verbs(res)
    if blurb:
        what = _one_line(blurb, _COMPACT_BLURB if compact else 110)
    elif compact:
        what = ", ".join(verbs[:_COMPACT_VERBS]) + (
            ", …" if len(verbs) > _COMPACT_VERBS else "")
        what = what or "no verbs known"
    else:
        what = "verbs: " + (", ".join(verbs) if verbs else "none known")
    line = f"- {res['tool']}: {what}"
    if not compact:
        return f"{line} [{_node_phrase(res)}]"
    if res["reason"] == peer_catalog.REASON_LOCAL:
        return line
    return f"{line} [{_node_phrase(res)}]"


def _more_tool(hidden: list[tuple[dict[str, Any], str | None]]) -> Tool:
    """The ``more`` call-through tool; its description is a bounded index of the
    discoverable domains, cut with a pointer to the full list when it overflows."""
    head = (
        "Reach an awm domain that is not listed as its own tool. "
        "more(domain, verb, args, peer) runs that domain's verb exactly as "
        "calling the domain directly would; verb='describe' returns the "
        "domain's verb schemas; more() with no domain lists them all. "
        "Discoverable domains:")
    description = head
    if not hidden:
        description += "\n(none)"
    for i, (res, blurb) in enumerate(hidden):
        line = _discoverable_line(res, blurb, compact=True)
        # Keep room for the overflow pointer unless this is the last line.
        reserve = 0 if i == len(hidden) - 1 else len(_MORE_OVERFLOW) + 1
        if len(description) + 1 + len(line) + reserve > _MORE_DESCRIPTION_CAP:
            description += "\n" + _MORE_OVERFLOW
            break
        description += "\n" + line
    return Tool(
        name=_MORE_TOOL,
        description=description,
        inputSchema={
            "type": "object",
            "properties": {
                "domain": {"type": "string",
                           "description": "The discoverable domain to call. "
                                          "Omit to list the domains."},
                "verb": {"type": "string",
                         "description": "The verb to run in that domain, or "
                                        "'describe' for its schemas."},
                "args": {"type": "object",
                         "description": "Arguments for the verb (see describe)."},
                "peer": {"type": "string",
                         "description": "Optional. Run on a specific fleet node; "
                                        "providersOf(tool=<domain>) lists them."},
            },
        },
        # A router: the target verb's own effect applies when it is invoked.
        **{"_meta": {"effect": "read"}},
    )


def _tier_split(local_verbs: dict[str, list[str]],
                snap: dict[str, dict[str, Any]],
                blurbs: dict[str, str]
                ) -> tuple[list[dict[str, Any]],
                           list[tuple[dict[str, Any], str | None]]]:
    """Fleet domains split by tier: ``(core resolutions, [(discoverable
    resolution, blurb)])``.

    A domain is core only if a local service folder declares it. Everything else
    the fleet can reach, a peer-only domain included, is discoverable. The walk is
    over domains something serves, so a core domain nothing serves is omitted."""
    core = _core_domains()
    shown: list[dict[str, Any]] = []
    hidden: list[tuple[dict[str, Any], str | None]] = []
    for domain in peer_catalog.fleet_domains(local_verbs, snap):
        res = peer_catalog.resolve(domain, local_verbs, snap)
        if domain in core:
            shown.append(res)
        else:
            hidden.append((res, blurbs.get(domain)))
    return shown, hidden


def _more_index(args: dict[str, Any]) -> str:
    """Answer ``more`` with no domain: the discoverable domains, one line each.

    A call that does name a domain never reaches the gateway as ``more`` — the MCP
    proxies rewrite it to the direct call, so the caller stamp and the gates see
    the true domain and verb. Reaching here with one means a caller skipped the
    rewrite, and running it unstamped would bypass those gates."""
    domain = args.get("domain")
    if domain not in (None, ""):
        if mcp_more.is_plain_domain(domain):
            raise ValueError(
                f"'{_MORE_TOOL}' with a domain is rewritten by the MCP proxy; "
                f"call {domain!r} directly with {{verb, args, peer}}")
        # Not a bare domain name (``kb@mira``, a path): the proxy refuses to
        # rewrite it, and it is no more a domain than any other unknown name.
        raise ValueError(f"Unknown tool: {domain}")
    domains = _domain_catalog()
    local_verbs = _local_domain_verbs(domains)
    _, hidden = _tier_split(local_verbs, peer_catalog.snapshot(), _domain_blurbs())
    lines = [_discoverable_line(res, blurb) for res, blurb in hidden]
    return "\n".join(lines) if lines else "No discoverable domains."


def list_domain_tools(*, peers: bool = False, tiers: bool = False,
                      grants: frozenset[str] | None = None) -> list[Tool]:
    """Project the collapsed per-domain MCP surface — one ``Tool`` per domain,
    each advertising the ``{verb, args, peer}`` envelope with ``verb`` enumerated.

    With ``peers=False`` (the default) this is **this node's** domains only, which
    is what a peer's edge serves when another node reads our catalog — the local
    view has to stay local or the fleet would advertise transitive peers nobody
    can dial.

    With ``peers=True`` (what the MCP proxy requests) the surface is the
    fleet-wide *union*: still one tool per domain name, but a domain only a peer
    runs appears too, each tool's description saying where it runs by default and
    where else it can be sent. That is the whole point — before this, a peer's
    catalog was merged under ``<domain>@<peer>`` names and two peers tripled the
    tool count for no new capability. The reserved ``providersOf`` tool is
    appended so the peer options are discoverable without listing them per tool.

    ``tiers=True`` (only with ``peers=True``) narrows that fleet surface to the
    **core** domains, ``providersOf`` and the ``more`` call-through tool, whose
    description indexes every other fleet domain. The filter lives here, after
    the fleet union and never in ``_domain_catalog``: dispatch, ``describe``,
    ``providersOf`` and the peers' own sweeps of our local view all read the
    unfiltered catalog, so a discoverable domain stays fully callable.

    ``grants`` (a foreign caller's read grants, see :func:`foreign_grants`) wins
    over ``peers`` and ``tiers``: the caller sees this node's domains only, cut to
    the verbs it may call, with no ``peer`` routing and no ``providersOf``.
    """
    catalog = _domain_catalog()
    if grants is not None:
        blurbs = _domain_blurbs()
        verbs_by_domain = _local_domain_verbs(_foreign_catalog(catalog, grants))
        return [Tool(name=domain,
                     description=_local_domain_description(
                         domain, verbs, blurbs.get(domain)),
                     inputSchema=_domain_envelope(verbs, with_peer=False))
                for domain, verbs in verbs_by_domain.items()]
    local_verbs = _local_domain_verbs(catalog)
    # A blurb is only ever this node's. A domain some peer provides is described
    # by that peer's own catalog, which we do not hold — and inventing one here
    # would put words in another node's mouth.
    blurbs = _domain_blurbs()

    if not peers:
        return [Tool(name=domain,
                     description=_local_domain_description(
                         domain, verbs, blurbs.get(domain)),
                     inputSchema=_domain_envelope(verbs))
                for domain, verbs in local_verbs.items()]

    snap = peer_catalog.snapshot()
    hidden: list[tuple[dict[str, Any], str | None]] = []
    if tiers:
        shown, hidden = _tier_split(local_verbs, snap, blurbs)
    else:
        shown = [peer_catalog.resolve(domain, local_verbs, snap)
                 for domain in peer_catalog.fleet_domains(local_verbs, snap)]
    out: list[Tool] = []
    for res in shown:
        out.append(Tool(name=res["tool"],
                        description=_fleet_domain_description(
                            res, blurbs.get(res["tool"])),
                        inputSchema=_domain_envelope(_advertised_verbs(res))))
    out.append(_providers_tool())
    if tiers:
        out.append(_more_tool(hidden))
    return out


def _local_domain_description(domain: str, verb_names: list[str],
                              blurb: str | None = None) -> str:
    return _with_blurb(
        f"Generic '{domain}' domain tool. Verbs: {', '.join(verb_names)}. "
        f"Call with {{verb, args}}; verb='describe' (optionally "
        f"args={{verb:<name>}}) returns full parameter schemas.",
        blurb)


def _with_blurb(generated: str, blurb: str | None) -> str:
    """Put the service's own prose FIRST, then the generated mechanics.

    Order is the whole point. The guidance has to be read before the verb list,
    or an agent has already decided which verb to call.
    """
    return f"{blurb} {generated}" if blurb else generated


def _advertised_verbs(res: dict[str, Any]) -> list[str]:
    """Verbs to enumerate for a fleet domain: the default provider's, exactly.

    With no default (several peers, no local) there is nothing authoritative to
    show, so take the union of the candidates — a superset is harmless (dispatch
    does not enforce the enum) while an arbitrary pick would be misleading.
    """
    providers = res.get("providers") or []
    default = res.get("default")
    for p in providers:
        if p["peer"] == default:
            return list(p["verbs"])
    seen: list[str] = []
    for p in providers:
        for v in p["verbs"]:
            if v not in seen:
                seen.append(v)
    return seen


def _fleet_domain_description(res: dict[str, Any],
                              blurb: str | None = None) -> str:
    """One domain's description in the fleet-wide view — where it runs, and where
    else it may be sent. Says it in prose because this is what the model reads to
    decide whether it needs ``peer`` at all."""
    domain = res["tool"]
    verbs = _advertised_verbs(res)
    others = [p["peer"] for p in res["providers"] if p["peer"] != res["default"]]
    head = (
        f"Generic '{domain}' domain tool. Verbs: {', '.join(verbs)}. "
        f"Call with {{verb, args}}; verb='describe' (optionally "
        f"args={{verb:<name>}}) returns full parameter schemas."
    )
    reason = res["reason"]
    if reason == peer_catalog.REASON_SINGLETON:
        tail = (f" Fleet singleton owned by '{res['default']}' — every call "
                f"routes there and peer= cannot redirect it.")
    elif reason == peer_catalog.REASON_LOCAL:
        tail = (f" Runs on this node by default"
                + (f"; also on {', '.join(others)} via peer=<name>." if others
                   else "."))
    elif reason == peer_catalog.REASON_SOLE_PEER:
        tail = (f" No local service provides it: runs on '{res['default']}', "
                f"the only provider, by default.")
    elif reason == peer_catalog.REASON_AMBIGUOUS:
        tail = (f" No local service provides it and several peers do "
                f"({', '.join(p['peer'] for p in res['providers'])}) — pass "
                f"peer=<name>; there is no default.")
    else:
        tail = ""
    return _with_blurb(head + tail, blurb)


def _describe_domain(domain: str, verb: str | None = None,
                     catalog: dict[str, list[dict[str, Any]]] | None = None) -> dict:
    """Answer ``verb='describe'`` from the catalog alone (no service round-trip).

    Returns ``{domain, description?, verbs:[{verb, description, params}]}`` where
    ``params`` is the verb's full ``inputSchema`` — the same schema the expanded
    per-verb tool advertised. ``verb`` narrows to a single entry. The top-level
    ``description`` is the service's own manifest prose about how the domain is
    meant to be used, present only when the service supplies one."""
    cat = catalog if catalog is not None else _domain_catalog()
    verbs = cat.get(domain)
    if verbs is None:
        raise ValueError(f"Unknown domain: {domain}")
    items = [v for v in verbs if verb is None or v["verb"] == verb]
    if verb is not None and not items:
        raise ValueError(f"Unknown verb {verb!r} for domain {domain!r}")
    blurb = _domain_blurbs().get(domain)
    return {
        "domain": domain,
        **({"description": blurb} if blurb else {}),
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
                           catalog: dict[str, list[dict[str, Any]]],
                           *, local_only: bool = False) -> str:
    """Dispatch a collapsed per-domain tool call (``{verb, args, peer}``).

    Routing comes first: the envelope's optional ``peer`` (or, absent one, the
    domain's default provider — this node for an ordinary domain, the owner for a
    declared singleton) decides *where* the verb runs. A peer target raises
    :class:`peer_catalog.PeerRedirect` carrying that peer's edge address, for the
    caller to dial directly — the gateway resolves and never relays, so no peer
    bytes traverse it. An unhonourable request raises ``ValueError`` naming the
    valid providers rather than quietly running here, which would be exactly the
    half-route failure the default-provider model exists to prevent.

    Then, locally: ``verb='describe'`` is answered from the catalog. A native verb
    routes through its Operation (same handler as the HTTP route + CLI command); a
    service verb is resolved back to its internal function via the existing
    ``_find_service_fn`` reverse lookup (so a name≠tool divergence like
    ``scope_refresh`` → internal ``awm_refresh`` still routes) and RPC'd with the
    ``as_`` placement identity threaded exactly as the flat path does.

    ``local_only`` (a foreign caller, whose ``catalog`` is already cut to the verbs
    it may call) skips the routing step: the verb runs here or not at all."""
    verb = args.get("verb")
    inner = args.get("args") or {}
    if not isinstance(inner, dict):
        raise ValueError("'args' must be an object")

    if not local_only:
        requested_peer = args.get("peer")
        if requested_peer is not None and not isinstance(requested_peer, str):
            raise ValueError("'peer' must be a node name string")
        target = peer_catalog.choose_target(
            name, requested_peer, _local_domain_verbs(catalog))
        if target != peer_catalog.LOCAL:
            raise peer_catalog.PeerRedirect(target, name, verb)

    if verb == _DESCRIBE_VERB:
        return _serialize(_describe_domain(name, inner.get("verb"), catalog))

    # Enforce the surface gate on dispatch, not just listing: a verb absent from
    # the (surface-filtered, and for a foreign caller grant-cut) domain catalog is
    # unknown here even if a matching function exists. What runs is the catalog
    # entry itself, never a second lookup that could land on another function.
    entry = next((v for v in catalog.get(name, []) if v["verb"] == verb), None)
    if entry is None:
        raise ValueError(f"Unknown verb {verb!r} for domain {name!r}")
    return await _run_entry(entry, inner, as_)


async def _run_entry(entry: dict[str, Any], args: dict, as_: str | None) -> str:
    """Run the native op or service function a catalog entry resolved to."""
    op = entry.get("op")
    if op is not None:
        with bound_caller(as_):
            if inspect.iscoroutinefunction(op.service_func):
                return _serialize(await _call_service(op, args))
            return _serialize(await run_in_threadpool(_call_service, op, args))
    return _serialize(await _rpc_call(entry["rec"], entry["fn"], args, as_))


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

    A domain **no local service provides** but a peer does routes here too, so a
    peer-only domain (``hpcllm``, ``orch``, …) is callable by its plain name
    rather than needing the ``<domain>@<peer>`` twin the surface used to carry.
    ``providersOf`` is checked first: it is a reserved top-level tool, not a
    domain verb.

    A foreign caller (``X-Awm-As`` naming a peer the book does not call domestic)
    goes through :func:`_dispatch_foreign` instead, which runs only the ``read``
    verbs in the categories its record grants.
    """
    args = args or {}
    grants = foreign_grants(as_)
    if grants is not None:
        return await _dispatch_foreign(name, args, as_, grants)
    if name == _PROVIDERS_TOOL:
        return _serialize(await _providers_of(
            args.get("tool"), bool(args.get("refresh"))))
    if name == _MORE_TOOL:
        return _serialize(_more_index(args))
    if "verb" in args:
        domains = _domain_catalog()
        if name in domains or name in peer_catalog.fleet_domains(
                _local_domain_verbs(domains)):
            return await _dispatch_domain(name, args, as_, domains)
    return await _dispatch_flat(name, args, as_)


async def _dispatch_flat(name: str, args: dict, as_: str | None) -> str:
    """Run the flat tool ``name``: a gateway-native op, else a service function."""
    op = _GATEWAY_OPS_BY_NAME.get(name)
    if op is not None:
        return await _run_entry({"op": op}, args, as_)

    rec, fn = _find_service_fn(name)
    if rec is None or fn is None:
        raise ValueError(f"Unknown tool: {name}")
    return await _run_entry({"rec": rec, "fn": fn}, args, as_)


def _foreign_more_index(domains: dict[str, list[dict[str, Any]]]) -> str:
    """``more`` with no domain, for a foreign caller: the domains it may call and
    only those verbs."""
    lines = [f"- {domain}: verbs: {', '.join(v['verb'] for v in verbs)}"
             for domain, verbs in sorted(domains.items())]
    return "\n".join(lines) if lines else "No discoverable domains."


async def _dispatch_foreign(name: str, args: dict, as_: str | None,
                            grants: frozenset[str]) -> str:
    """Dispatch for a foreign caller: only ``read`` verbs in a granted category.

    Every refusal is ``Unknown tool`` (→ 404), the answer for a name that does not
    exist, so the caller learns nothing about the verbs it may not call. The call
    always runs on this node: ``providersOf`` and any ``peer`` argument are
    refused, because a foreign node gets no onward hops through us. The
    ``describe`` verb and ``more`` see the same cut catalog as ``/tools``."""
    if not isinstance(name, str) or not isinstance(args, dict):
        raise ValueError(f"Unknown tool: {name}")
    if name == _PROVIDERS_TOOL or _has_peer_arg(args):
        raise ValueError(f"Unknown tool: {name}")
    domains = _foreign_catalog(_domain_catalog(), grants)
    if name == _MORE_TOOL:
        domain = args.get("domain")
        if domain in (None, ""):
            return _foreign_more_index(domains)
        if not mcp_more.is_plain_domain(domain):
            raise ValueError(f"Unknown tool: {domain}")
        rewritten, forwarded = mcp_more.rewrite_call(name, args)
        return await _dispatch_foreign(rewritten, forwarded, as_, grants)
    if "verb" in args and name in domains:
        if not isinstance(args["verb"], str):
            raise ValueError(f"Unknown tool: {name}")
        try:
            return await _dispatch_domain(name, args, as_, domains, local_only=True)
        except rpc.RpcError as exc:
            raise _refusal_as_unknown(
                exc, f"Unknown verb {args['verb']!r} for domain {name!r}") from None
    entry = _flat_entries().get(name)
    if entry is None or not _allowed(entry["effect"], entry["category"], grants):
        raise ValueError(f"Unknown tool: {name}")
    try:
        return await _run_entry(entry, args, as_)
    except rpc.RpcError as exc:
        raise _refusal_as_unknown(exc, f"Unknown tool: {name}") from None


def _refusal_as_unknown(exc: rpc.RpcError, unknown: str) -> Exception:
    """A service's own ``PermissionError`` for a foreign caller, as the 404 an
    unknown tool gets; any other service error is returned unchanged.

    A verb whose category the peer holds can still refuse part of what it serves
    (scopes shows a peer journals but not messages). That refusal must look like
    the gate's, or its message tells the peer which verbs and kinds exist."""
    if exc.error_class == "PermissionError":
        return ValueError(unknown)
    return exc
