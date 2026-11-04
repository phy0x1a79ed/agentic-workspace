"""Live service catalog — the single source of the tools the gateway exposes.

The gateway is the sole interface + coordination hub. Every callable surface it
re-exposes (MCP / CLI / HTTP) is assembled here from two inputs and rendered to
all three surfaces by the (unchanged) ``operations.py`` compiler:

1. **Native ops** — a static table of operations the gateway owns itself
   (``awm_status``, ``awm_services_*``, ``operations_peer_*``, ...), published
   as verbs of the ``awm`` and ``operations`` tools. Dispatched to local
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
      "no_response": false,
      "timeout": <seconds>?,
      "returns_files": false }              # result's files[].path go back to a peer caller

A param may carry ``"file": "in"``: its value is a local path that a caller on
another node sends along with the call (see :mod:`awm.gateway.filehop`).

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

from awm.config import edges, verb_effect
from awm.gateway import aliases, filehop, peer_catalog, private_gate, surface, usage
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
# registry (the gateway's own control plane: status / restart /
# hub list+deregister / services lifecycle). Their MCP tools + HTTP routes +
# CLI commands all come from one Operation each — see gateway_ops.py.
# ---------------------------------------------------------------------------

# MCP-surface gateway tools, keyed by name for dispatch + uniqueness reservation.
# A native verb hidden from MCP (no "mcp" surface) still runs by its flat name
# over HTTP, so the dispatch table holds every op with an agent or HTTP surface.
_GATEWAY_MCP_TOOLS: list[Tool] = operations_to_mcp_tools(GATEWAY_OPERATIONS)
_GATEWAY_OPS_BY_NAME = {
    op.name: op for op in GATEWAY_OPERATIONS if op.surfaces & {"mcp", "http"}
}

usage.set_renames(aliases.usage_renames())


# ---------------------------------------------------------------------------
# Registered-service ops — projected live from the hub registry
# ---------------------------------------------------------------------------


def _fn_on_surface(fn: dict, where: str) -> bool:
    """Whether a manifest function is projected onto ``where`` ("mcp"/"cli"/"http").

    Honors the per-function ``"surfaces"`` field (see the module docstring's
    registration contract). A function that omits ``surfaces`` is on all three;
    one declaring ``["cli", "http"]`` is hidden from MCP but still runs from the
    CLI and over HTTP. Only the per-tool MCP projection consults this;
    ``list_tools()`` stays unfiltered so the CLI generator and flat ``/invoke``
    by-name dispatch keep every verb."""
    surfaces = fn.get("surfaces")
    if not surfaces:
        return True
    return where in surfaces


def _tool_name(rec: ServiceRecord, fn: dict) -> str:
    """The published name of a service function: its manifest ``"tool"`` key, else
    ``{service}_{name}``.

    ``name`` stays the service-internal handle that ``/svc/<service>/fn/<name>``
    dispatches on, while the published name is what every surface shows. Under the
    registration rule (see ``surface``) it is ``<tool>_<verb>``, where ``<tool>``
    is the tool the service's manifest names. :func:`list_tools` enforces global
    uniqueness by warn-and-skip, so two services must not publish one name."""
    return surface.published_name(rec.name, fn)


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
        if p.get("file") == "in":
            prop[filehop.FILE_KEY] = "in"
        props[p["name"]] = prop
        if p.get("required"):
            required.append(p["name"])
    schema: dict[str, Any] = {"type": "object", "properties": props}
    if required:
        schema["required"] = required
    if fn.get("returns_files"):
        schema[filehop.RETURNS_KEY] = True
    if fn.get("timeout"):
        schema[filehop.TIMEOUT_KEY] = float(fn["timeout"])
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


def _fn_effect(fn: dict) -> str:
    """The effect a manifest function declares. An effect the book of effects
    does not know counts as ``write``: a typo must not open a verb."""
    try:
        return verb_effect(fn)
    except ValueError:
        log.warning("function %r declares an unknown effect; treating it as write",
                    fn.get("name"))
        return "write"


def _flat_entries() -> dict[str, dict[str, Any]]:
    """``{flat tool name: entry}`` with the precedence ``dispatch`` and
    :func:`list_tools` use: native ops first, then the first registrant.

    An entry is ``{effect, op, rec, fn}`` where exactly one of ``op`` and
    ``rec``/``fn`` is set: the thing the gate approves is the thing that runs."""
    out: dict[str, dict[str, Any]] = {
        name: {"effect": op.effect, "op": op, "rec": None, "fn": None}
        for name, op in _GATEWAY_OPS_BY_NAME.items()}
    for rec in get_registry().service_records():
        for fn in (rec.api or {}).get("functions", []) or []:
            if isinstance(fn, dict) and fn.get("name"):
                out.setdefault(_tool_name(rec, fn), {
                    "effect": _fn_effect(fn), "op": None, "rec": rec, "fn": fn["name"]})
    return out


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
# Per-domain projection (the collapsed MCP read surface)
# ---------------------------------------------------------------------------
# The expanded ``list_tools()`` projection above stays exactly as-is (the CLI
# generator and the flat ``/invoke`` dispatch both depend on it). This second,
# *parallel* projection folds that same surface by **tool**: each service
# publishes under the ``tool`` its manifest names, and a published name is
# ``<tool>_<verb>`` (see ``surface``). Gateway-native ops group by their ``tool``
# and ``verb`` (their CLI group and command, when those are not set). The MCP
# stdio proxy requests this view, so a client carries a few dozen generic tools instead of
# hundreds of verb-tools; verbs and their full param schemas come from the
# ``describe`` verb.
#
# ``?view=domains&peers=1`` widens the same projection to the **fleet**: still one
# tool per name, but a tool only a peer runs appears too, and the envelope's
# ``peer`` key chooses the node. Which node a call lands on by default is
# ``peer_catalog``'s decision, not this module's.
#
# A tool is off the MCP surface when its service's ``tier`` is ``off``, and a verb
# is when its manifest entry omits ``mcp`` from ``surfaces``. Both stay on the
# flat projection, so the CLI and HTTP still reach them.

_DESCRIBE_VERB = "describe"

#: Verbs ``awm describe`` lists when it is given no limit.
DESCRIBE_LIMIT = 40

#: The tool whose ``describe`` is the fleet overview rather than its own schemas.
OVERVIEW_TOOL = "awm"

_ENVELOPE_KEYS = frozenset({"verb", "args", "peer"})
_VERB_LINE_CHARS = 100

# The envelope every tool advertises. The rich per-verb schema comes from
# ``describe``; the ``verb`` and ``peer`` enums are stamped per tool.
_DOMAIN_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "verb": {
            "type": "string",
            "description": "The operation to run. verb='describe' (optionally "
                           "args={verbs:[<name>, ...]}) returns parameter "
                           "schemas.",
        },
        "args": {
            "type": "object",
            "description": "Arguments for the chosen verb. An unknown argument "
                           "is an error that returns the verb's schema.",
        },
        "peer": {
            "type": "string",
            "description": "Optional. The fleet node to run this verb on.",
        },
    },
    "required": ["verb"],
}


class ArgumentError(ValueError):
    """A call named an argument its verb does not declare. ``payload`` is the
    structured error body, schema included."""

    def __init__(self, message: str, payload: dict[str, Any]) -> None:
        super().__init__(message)
        self.payload = payload


def _split_name(rec: ServiceRecord, fn: dict) -> tuple[str, str]:
    """``(tool, verb)`` a manifest function is published as."""
    tool, verb = surface.split_published(rec.name, _tool_name(rec, fn))
    return tool, verb or tool


def _mcp_visible(rec: ServiceRecord, fn: dict) -> bool:
    return (_fn_on_surface(fn, "mcp")
            and discovery.service_tier(rec.name) != discovery.TIER_OFF)


def _domain_catalog() -> dict[str, list[dict[str, Any]]]:
    """Fold the live (native + service) tool surface into ``{tool: [verb…]}``.

    Each verb entry is ``{"verb": str, "tool": Tool}`` — the ``Tool`` carries the
    full per-verb description + ``inputSchema`` (byte-identical to the expanded
    projection), reused verbatim by :func:`_describe_domain`. Native ops are
    folded first so a service can never shadow a gateway-native verb (first-wins,
    mirroring ``list_tools``'s duplicate handling). Each entry also carries the
    verb's declared ``effect`` and the ``op`` or ``rec``/``fn`` it resolves to,
    so dispatch runs exactly the entry that was approved. Sync over a GIL-safe
    registry snapshot — same contract as ``list_tools``."""
    domains: dict[str, list[dict[str, Any]]] = {}
    seen: set[tuple[str, str]] = set()

    def _add(domain: str, verb: str, tool: Tool, *, origin: str,
             effect: str, op: Any = None,
             rec: ServiceRecord | None = None, fn: str | None = None) -> None:
        key = (domain, verb)
        if not (domain and verb):
            log.warning(
                "unnamed domain verb %r/%r from %s — skipping", domain, verb, origin)
            return
        if key in seen:
            log.warning(
                "duplicate domain verb %s/%s from %s — skipping", domain, verb, origin)
            return
        seen.add(key)
        domains.setdefault(domain, []).append(
            {"verb": verb, "tool": tool, "effect": effect,
             "op": op, "rec": rec, "fn": fn})

    for op in GATEWAY_OPERATIONS:
        if "mcp" not in op.surfaces:
            continue
        _add(op.mcp_tool, op.mcp_verb, _to_mcp_tool(op), origin="gateway-native",
             effect=op.effect, op=op)

    for rec in get_registry().service_records():
        for fn in (rec.api or {}).get("functions", []) or []:
            if not (isinstance(fn, dict) and fn.get("name")):
                continue
            if not _mcp_visible(rec, fn):
                continue
            domain, verb = _split_name(rec, fn)
            _add(domain, verb, _fn_to_tool(rec, fn), origin=f"service {rec.name!r}",
                 effect=_fn_effect(fn), rec=rec, fn=fn["name"])

    return domains


def _domain_blurbs() -> dict[str, str]:
    """``{tool: prose}`` from each service's optional manifest ``description``.

    Everything else the surface shows about a tool is generated, so a service had
    no way to say how it is meant to be USED, only what it can do. The key is
    additive: a manifest without it is unchanged. A service publishing under
    several tools gets its blurb on each; a tool with no MCP-visible verb gets
    nothing, since it has no tool to describe."""
    out: dict[str, str] = {}
    for rec in get_registry().service_records():
        api = rec.api or {}
        blurb = api.get("description")
        if not isinstance(blurb, str) or not blurb.strip():
            continue
        for fn in api.get("functions", []) or []:
            if not (isinstance(fn, dict) and fn.get("name")):
                continue
            if not _mcp_visible(rec, fn):
                continue
            out.setdefault(_split_name(rec, fn)[0], blurb.strip())
    return out


def _peer_phrase(res: dict[str, Any] | None) -> tuple[str, list[str] | None, bool]:
    """``(description, enum, required)`` for a tool's ``peer`` argument. ``res``
    is the tool's provider resolution; ``None`` means a node-local view."""
    if res is None:
        return ("Optional. The fleet node to run this verb on; default is this "
                "node.", None, False)
    valid = [p["peer"] for p in res["providers"]]
    listing = f" Valid: {', '.join(valid)} ('{peer_catalog.LOCAL}' is this node)."
    reason = res["reason"]
    if reason == peer_catalog.REASON_SINGLETON:
        return (f"Optional. Default and only valid node: {res['default']} "
                f"(a fleet singleton).", valid, False)
    if reason == peer_catalog.REASON_LOCAL:
        return (f"Optional. Default: {peer_catalog.LOCAL} (this node)." + listing,
                valid, False)
    if reason == peer_catalog.REASON_SOLE_PEER:
        return (f"Optional. Default: {res['default']}, the only node serving it."
                + listing, valid, False)
    return ("Required: this node does not serve it and several nodes do." + listing,
            valid, True)


def _domain_envelope(verb_names: list[str], *,
                     res: dict[str, Any] | None = None) -> dict[str, Any]:
    """The ``{verb, args, peer}`` envelope schema, with ``verb`` enumerated and,
    given the tool's provider resolution ``res``, ``peer`` enumerated.

    Deep-copied — a shallow copy shares the nested ``properties`` dict, so
    stamping the enum would mutate the module constant for every later tool.

    The enums are discovery, not enforcement: dispatch validates a verb against the
    live catalog (locally) or lets the target peer validate its own, so a snapshot
    that is a couple of minutes stale can never block a call that would work. The
    ``peer`` enum is as of the session's launch.
    """
    schema = copy.deepcopy(_DOMAIN_INPUT_SCHEMA)
    text, enum, required = _peer_phrase(res)
    schema["properties"]["peer"]["description"] = text
    if enum:
        schema["properties"]["peer"]["enum"] = enum
    if required:
        schema["required"] = ["verb", "peer"]
    if verb_names:
        schema["properties"]["verb"]["enum"] = [*verb_names, _DESCRIBE_VERB]
    return schema


def _local_domain_verbs(
        catalog: dict[str, list[dict[str, Any]]]) -> dict[str, list[str]]:
    """``{tool: [verb…]}`` for this node — the shape ``peer_catalog`` reasons
    over, so it never has to import this module back."""
    return {domain: [v["verb"] for v in verbs] for domain, verbs in catalog.items()}


async def _choose_target(name: str, requested: str | None,
                         catalog: dict[str, list[dict[str, Any]]]) -> str:
    """``peer_catalog.choose_target``, but a refusal that only reflects a peer
    catalog this node has not read yet waits for one sweep instead of answering
    "does not provide"."""
    local = _local_domain_verbs(catalog)
    try:
        return peer_catalog.choose_target(name, requested, local)
    except ValueError:
        peer = (requested or "").strip()
        if not peer or peer_catalog.catalog_pending(peer) is None:
            raise
    why = await peer_catalog.load_pending(peer)
    if why:
        raise ValueError(
            f"this node has not loaded the tool catalog of peer {peer!r} yet ({why}), "
            f"so it cannot route {name!r} there. "
            + ("This gateway just restarted: retry in about 10 seconds."
               if peer_catalog.warming_up() else
               f"Peer {peer!r} may be asleep or off the network."))
    return peer_catalog.choose_target(name, requested, local)


def _without_verbs(res: dict[str, Any]) -> dict[str, Any]:
    """``res`` with each provider cut to its name, reachability and any error."""
    providers = [
        {k: v for k, v in p.items() if k != "verbs" and not (k == "error" and v is None)}
        for p in res["providers"]
    ]
    return {**res, "providers": providers}


async def _providers_of(tool: str | None = None, refresh: bool = False,
                        verbs: bool = False) -> dict[str, Any]:
    """Where a tool can run, and where it goes by default.

    Backs the ``peer providers`` Operation. ``refresh`` forces a synchronous peer
    sweep; without it this reads the background snapshot and never touches the
    network. Omit ``tool`` for the whole fleet map. ``verbs`` adds each provider's
    verb list, which costs a few KB per tool and answers a question
    ``<tool>(verb="describe")`` answers better.

    An unreachable peer is reported as unreachable with its last-known tools,
    never dropped: "mira does not have this" and "mira is asleep" are different
    answers and conflating them is what makes a discovery tool useless.
    """
    if refresh:
        await peer_catalog.sweep()
    snap = peer_catalog.snapshot()
    local = _local_domain_verbs(_domain_catalog())
    swept = peer_catalog.swept_at()
    shape = (lambda res: res) if verbs else _without_verbs
    if tool:
        return {**shape(peer_catalog.resolve(tool, local, snap)), "swept_at": swept}
    return {
        "swept_at": swept,
        "tools": [shape(peer_catalog.resolve(d, local, snap))
                  for d in peer_catalog.fleet_domains(local, snap)],
    }


def _one_line(text: str, limit: int = 110) -> str:
    """First sentence of ``text`` on one line, capped at ``limit`` characters."""
    flat = " ".join(text.split())
    head, sep, _ = flat.partition(". ")
    flat = head + ("." if sep else "")
    return flat if len(flat) <= limit else flat[: limit - 1].rstrip() + "…"


def _required(tool: Tool) -> list[str]:
    return list((tool.inputSchema or {}).get("required") or [])


def _verb_line(entry: dict[str, Any]) -> str:
    required = ", ".join(_required(entry["tool"]))
    return (f"- {entry['verb']}({required}): "
            f"{_one_line(entry['tool'].description or '', _VERB_LINE_CHARS)}")


def _local_domain_description(domain: str, entries: list[dict[str, Any]],
                              blurb: str | None = None) -> str:
    """A tool's description: the service's own prose first, then one line per
    verb with its required arguments."""
    return _with_blurb(_generated_description(domain, entries), blurb)


def _generated_description(domain: str, entries: list[dict[str, Any]]) -> str:
    lines = "\n".join(_verb_line(e) for e in entries)
    return (f"The '{domain}' tool. Call with {{verb, args}}; verb='describe' "
            f"(optionally args={{verbs:[<name>, ...]}}) returns full parameter "
            f"schemas. Verbs, with required arguments:\n{lines}")


def _with_blurb(generated: str, blurb: str | None) -> str:
    """Put the service's own prose FIRST, then the generated mechanics.

    Order is the whole point. The guidance has to be read before the verb list,
    or an agent has already decided which verb to call.
    """
    return f"{blurb} {generated}" if blurb else generated


def _advertised_verbs(res: dict[str, Any]) -> list[str]:
    """Verbs to enumerate for a fleet tool: the default provider's, exactly.

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
                              entries: list[dict[str, Any]] | None,
                              blurb: str | None = None) -> str:
    """One tool's description in the fleet-wide view — what it does, and where it
    runs. A tool this node serves is described from the local catalog; one only a
    peer serves carries that peer's own description."""
    domain = res["tool"]
    if entries:
        head = _local_domain_description(domain, entries, blurb)
    else:
        carried = peer_catalog.description_for(res)
        if carried:
            head = carried
        else:
            head = _with_blurb(
                f"The '{domain}' tool. Call with {{verb, args}}; verb='describe' "
                f"returns full parameter schemas. Verbs: "
                f"{', '.join(_advertised_verbs(res))}.", blurb)
    others = [p["peer"] for p in res["providers"] if p["peer"] != res["default"]]
    reason = res["reason"]
    if reason == peer_catalog.REASON_SINGLETON:
        tail = (f" Fleet singleton owned by '{res['default']}' — every call "
                f"routes there and peer= cannot redirect it.")
    elif reason == peer_catalog.REASON_LOCAL:
        tail = (" Runs on this node by default"
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
    return head + tail


def list_domain_tools(*, peers: bool = False) -> list[Tool]:
    """Project the collapsed MCP surface — one ``Tool`` per tool name, each
    advertising the ``{verb, args, peer}`` envelope with ``verb`` enumerated.

    With ``peers=False`` (the default) this is **this node's** tools only, which
    is what a peer's edge serves when another node reads our catalog — the local
    view has to stay local or the fleet would advertise transitive peers nobody
    can dial.

    With ``peers=True`` (what the MCP proxy requests) the surface is the
    fleet-wide *union*: still one tool per name, but a tool only a peer runs
    appears too, each description saying where it runs by default and each
    ``peer`` argument enumerating the nodes that serve it. Every tool is
    top-level; nothing is held back behind an index.
    """
    catalog = _domain_catalog()
    blurbs = _domain_blurbs()
    local_verbs = _local_domain_verbs(catalog)

    if not peers:
        return [Tool(name=domain,
                     description=_local_domain_description(
                         domain, entries, blurbs.get(domain)),
                     inputSchema=_domain_envelope([e["verb"] for e in entries]))
                for domain, entries in catalog.items()]

    snap = peer_catalog.snapshot()
    out: list[Tool] = []
    for domain in peer_catalog.fleet_domains(local_verbs, snap):
        res = peer_catalog.resolve(domain, local_verbs, snap)
        out.append(Tool(
            name=domain,
            description=_fleet_domain_description(
                res, catalog.get(domain), blurbs.get(domain)),
            inputSchema=_domain_envelope(_advertised_verbs(res), res=res)))
    return out


def _describe_domain(domain: str, verbs: list[str] | str | None = None,
                     catalog: dict[str, list[dict[str, Any]]] | None = None) -> dict:
    """Answer ``verb='describe'`` from the catalog alone (no service round-trip).

    Returns ``{domain, description?, verbs:[...]}``, each verb trimmed by
    :func:`_describe_verb`. ``verbs`` (a list, or one name) narrows the answer;
    none lists every visible verb. The top-level ``description`` is the service's
    own manifest prose, present only when the service supplies one."""
    cat = catalog if catalog is not None else _domain_catalog()
    entries = cat.get(domain)
    if entries is None:
        raise ValueError(f"Unknown domain: {domain}")
    wanted = [verbs] if isinstance(verbs, str) else verbs
    if wanted:
        if not all(isinstance(v, str) for v in wanted):
            raise ValueError("'verbs' must be a list of verb names")
        unknown = [v for v in wanted if all(e["verb"] != v for e in entries)]
        if unknown:
            raise ValueError(f"Unknown verb(s) {unknown!r} for domain {domain!r}")
        entries = [e for e in entries if e["verb"] in wanted]
    blurb = _domain_blurbs().get(domain)
    return {
        "domain": domain,
        **({"description": blurb} if blurb else {}),
        "verbs": [_describe_verb(e["verb"], e["tool"]) for e in entries],
    }


def call_spec(name: str, args: dict) -> dict[str, Any]:
    """The file-hop spec (:func:`filehop.spec_from_schema`) of the verb a call
    names, domain-shaped or flat. Empty when the name is unknown here."""
    schema = None
    verb = args.get("verb") if isinstance(args, dict) else None
    if verb:
        for v in _domain_catalog().get(name, []):
            if v["verb"] == verb:
                schema = v["tool"].inputSchema
    else:
        schema = next((t.inputSchema for t in list_tools() if t.name == name), None)
    return filehop.spec_from_schema(schema)


def describe_overview(limit: int = DESCRIBE_LIMIT,
                      catalog: dict[str, list[dict[str, Any]]] | None = None) -> dict:
    """The one-call overview behind ``awm describe``: the ``limit`` most used
    verbs of the last 60 days, each with a one-line summary and its required
    arguments, plus one line for every tool in the fleet.

    The best verb of every local tool is always listed, so a tool nobody has used
    still shows up and the list can run past ``limit`` when there are more tools.
    A tool only a peer serves appears in ``tools`` alone: its verbs' summaries
    live on that node."""
    cat = catalog if catalog is not None else _domain_catalog()
    counts = usage.totals(resolve=aliases.renamed_pair)
    rows = [(domain, e) for domain, entries in cat.items() for e in entries]
    scored = sorted(rows, key=lambda r: -counts.get((r[0], r[1]["verb"]), 0))
    chosen: list[tuple[str, dict[str, Any]]] = []
    covered: set[str] = set()
    for row in scored:
        if row[0] not in covered:
            covered.add(row[0])
            chosen.append(row)
    taken = {id(r[1]) for r in chosen}
    for row in scored:
        if len(chosen) >= limit:
            break
        if id(row[1]) not in taken:
            chosen.append(row)
    chosen.sort(key=lambda r: -counts.get((r[0], r[1]["verb"]), 0))

    local_verbs = _local_domain_verbs(cat)
    snap = peer_catalog.snapshot()
    blurbs = _domain_blurbs()
    tools = []
    for domain in peer_catalog.fleet_domains(local_verbs, snap):
        res = peer_catalog.resolve(domain, local_verbs, snap)
        summary = blurbs.get(domain) or peer_catalog.description_for(res) or ""
        tools.append({"tool": domain, "summary": _one_line(summary),
                      "node": res["default"], "verbs": len(_advertised_verbs(res))})
    return {
        "verbs": [{"tool": d, "verb": e["verb"],
                   "summary": _one_line(e["tool"].description or ""),
                   "required": _required(e["tool"]),
                   "uses": counts.get((d, e["verb"]), 0)} for d, e in chosen],
        "tools": tools,
        "omitted": max(0, len(rows) - len(chosen)),
        "next": "describe(tool=<name>) lists every verb of one tool, "
                "optionally narrowed with verbs=[...]",
    }


def _answer_describe(name: str, inner: dict, catalog: dict) -> dict:
    """``describe`` on tool ``name``. On the ``awm`` tool with no ``tool``
    argument it is the ranked overview of the whole surface (``limit`` caps it);
    given ``tool`` it describes that tool; elsewhere it describes ``name``."""
    verbs = inner.get("verbs") or inner.get("verb")
    if name != OVERVIEW_TOOL:
        return _describe_domain(name, verbs, catalog)
    target = inner.get("tool")
    if target:
        if not isinstance(target, str):
            raise ValueError("'tool' must be a tool name")
        return _describe_domain(target, verbs, catalog)
    limit = inner.get("limit", DESCRIBE_LIMIT)
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
        raise ValueError("'limit' must be a positive integer")
    return describe_overview(limit, catalog)


def _describe_verb(verb: str, tool: Tool) -> dict[str, Any]:
    """One verb as a caller needs it: params keyed by name, ``type`` only when
    it is not a plain string, and the required names. Empty keys are left out."""
    schema = tool.inputSchema or {}
    params = {name: _describe_param(prop)
              for name, prop in (schema.get("properties") or {}).items()}
    required = list(schema.get("required") or [])
    return {
        "verb": verb,
        **({"description": tool.description} if tool.description else {}),
        **({"params": params} if params else {}),
        **({"required": required} if required else {}),
        **({"returns_files": True} if schema.get(filehop.RETURNS_KEY) else {}),
    }


def _describe_param(prop: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    kind = _param_type(prop)
    if kind != "string":
        out["type"] = kind
    if prop.get("description"):
        out["description"] = prop["description"]
    enum = prop.get("enum") or next(
        (b["enum"] for b in prop.get("anyOf") or [] if b.get("enum")), None)
    if enum:
        out["enum"] = enum
    if prop.get("default") is not None:
        out["default"] = prop["default"]
    if prop.get(filehop.FILE_KEY):
        out["file"] = prop[filehop.FILE_KEY]
    return out


def _param_type(prop: dict[str, Any]) -> str:
    """A compact type name. Null branches drop out, so an optional string
    (``["string", "null"]`` or a pydantic ``anyOf``) reads as ``string``."""
    if "$ref" in prop:
        return prop["$ref"].rsplit("/", 1)[-1]
    branches = prop.get("anyOf") or prop.get("oneOf")
    if branches:
        kinds = [_param_type(b) for b in branches if b.get("type") != "null"]
    else:
        raw = prop.get("type", "string")
        kinds = [k for k in (raw if isinstance(raw, list) else [raw]) if k != "null"]
        kinds = [f"array[{_param_type(prop.get('items') or {})}]" if k == "array"
                 else k for k in kinds]
    return "|".join(dict.fromkeys(kinds)) or "string"


def _find_native_op(domain: str, verb: str):
    """Locate the MCP-surface gateway-native Operation for a tool/verb pair, else
    ``None``."""
    for op in GATEWAY_OPERATIONS:
        if "mcp" in op.surfaces and op.mcp_tool == domain and op.mcp_verb == verb:
            return op
    return None


def _spec_of(rec: ServiceRecord, fn: str) -> dict | None:
    for spec in (rec.api or {}).get("functions", []) or []:
        if isinstance(spec, dict) and spec.get("name") == fn:
            return spec
    return None


def _check_arguments(rec: ServiceRecord, fn: str, args: dict) -> None:
    """Refuse an argument the verb does not declare, answering with its schema.

    Names starting with an underscore are the gateway's own (``_caller_pid``). A
    verb whose manifest entry declares no ``params`` accepts anything: absence of
    a schema is not a promise of no arguments."""
    spec = _spec_of(rec, fn)
    if spec is None or spec.get("params") is None:
        return
    declared = {p.get("name") for p in spec["params"] if isinstance(p, dict)}
    unknown = sorted(k for k in args if k not in declared
                     and not (isinstance(k, str) and k.startswith("_")))
    if not unknown:
        return
    published = _tool_name(rec, spec)
    schema = _fn_to_tool(rec, spec).inputSchema
    raise ArgumentError(
        f"unknown argument(s) {unknown} for {published}",
        {"error_class": "UnknownArgument", "tool": published,
         "error": f"unknown argument(s) {unknown} for {published}; the verb takes "
                  f"{sorted(n for n in declared if n)}",
         "unknown": unknown, "schema": schema})


def _check_envelope(name: str, args: dict, entry: dict[str, Any] | None) -> None:
    """Refuse a key beside ``verb``, ``args`` and ``peer`` in a tool call: a verb
    argument put at the top level would otherwise be dropped without a word."""
    extra = sorted(k for k in args
                   if k not in _ENVELOPE_KEYS
                   and not (isinstance(k, str) and k.startswith("_")))
    if not extra:
        return
    schema = entry["tool"].inputSchema if entry else _DOMAIN_INPUT_SCHEMA
    raise ArgumentError(
        f"unknown key(s) {extra} in a call to {name!r}",
        {"error_class": "UnknownArgument", "tool": name, "unknown": extra,
         "error": f"unknown key(s) {extra} in a call to {name!r}; a tool takes "
                  f"{{verb, args, peer}}, and a verb's arguments go inside args",
         "schema": schema})


async def _dispatch_domain(name: str, args: dict, as_: str | None,
                           catalog: dict[str, list[dict[str, Any]]]) -> str:
    """Dispatch a collapsed per-tool call (``{verb, args, peer}``).

    Routing comes first: the envelope's optional ``peer`` (or, absent one, the
    tool's default provider — this node for an ordinary tool, the owner for a
    declared singleton, the one serving node when only one serves it) decides
    *where* the verb runs. A peer target raises :class:`peer_catalog.PeerRedirect`
    carrying that peer's edge address, for the caller to dial directly — the
    gateway resolves and never relays, so no peer bytes traverse it. An
    unhonourable request raises ``ValueError`` naming the valid providers rather
    than quietly running here, which would be exactly the half-route failure the
    default-provider model exists to prevent.

    Then, locally: ``verb='describe'`` is answered from the catalog. A native verb
    routes through its Operation (same handler as the HTTP route + CLI command); a
    service verb is resolved back to its internal function via the existing
    ``_find_service_fn`` reverse lookup and RPC'd with the ``as_`` placement
    identity threaded exactly as the flat path does."""
    verb = args.get("verb")
    inner = args.get("args") or {}
    if not isinstance(inner, dict):
        raise ValueError("'args' must be an object")

    requested_peer = args.get("peer")
    if requested_peer is not None and not isinstance(requested_peer, str):
        raise ValueError("'peer' must be a node name string")
    target = await _choose_target(name, requested_peer, catalog)
    if target != peer_catalog.LOCAL:
        raise peer_catalog.PeerRedirect(target, name, verb)

    entry = next((v for v in catalog.get(name, []) if v["verb"] == verb), None)
    _check_envelope(name, args, entry)

    if verb == _DESCRIBE_VERB:
        return _serialize(_answer_describe(name, inner, catalog))

    # Enforce the surface gate on dispatch, not just listing: a verb absent from
    # the surface-filtered catalog is
    # unknown here even if a matching function exists. What runs is the catalog
    # entry itself, never a second lookup that could land on another function.
    if entry is None:
        raise ValueError(f"Unknown verb {verb!r} for domain {name!r}")
    usage.record(name, entry["verb"])
    return await _run_entry(entry, inner, as_,
                            unknown=f"Unknown verb {verb!r} for domain {name!r}")


async def _run_entry(entry: dict[str, Any], args: dict, as_: str | None,
                     *, unknown: str) -> str:
    """Run the native op or service function a catalog entry resolved to.

    A peer caller whose arguments name a private project, scope or path is
    refused here with ``unknown``, the error the call would get if it named
    nothing that exists (see :mod:`private_gate`)."""
    if private_gate.refuses(as_, args):
        raise ValueError(unknown)
    op = entry.get("op")
    if op is not None:
        with bound_caller(as_):
            if inspect.iscoroutinefunction(op.service_func):
                return _serialize(await _call_service(op, args))
            return _serialize(await run_in_threadpool(_call_service, op, args))
    _check_arguments(entry["rec"], entry["fn"], args)
    return _serialize(await _rpc_call(entry["rec"], entry["fn"], args, as_))


def published_name_of(service: str, fn: str) -> str:
    """The flat name the registered ``service``'s internal function ``fn`` is
    published as; ``<service>_<fn>`` when it is not registered."""
    for rec in get_registry().service_records():
        if rec.name != service:
            continue
        for spec in (rec.api or {}).get("functions", []) or []:
            if isinstance(spec, dict) and spec.get("name") == fn:
                return _tool_name(rec, spec)
    return f"{service}_{fn}"


def _flat_names() -> set[str]:
    """Every flat name the catalog can run: native ops and published service
    functions, hidden ones included."""
    names = set(_GATEWAY_OPS_BY_NAME)
    for rec in get_registry().service_records():
        for fn in (rec.api or {}).get("functions", []) or []:
            if isinstance(fn, dict) and fn.get("name"):
                names.add(_tool_name(rec, fn))
    return names


def resolve_alias(name: str, args: dict) -> tuple[str, dict]:
    """The call a request becomes once an old name is replaced by its successor.

    Idempotent, and the identity for a name that is registered or has no row, or
    whose successor is absent."""
    if not isinstance(name, str) or not isinstance(args, dict):
        return name, args

    def visible(tool: str, verb: str) -> bool:
        return any(e["verb"] == verb for e in _domain_catalog().get(tool, []))

    return aliases.resolve(name, args, flat_names=_flat_names, visible=visible)


async def dispatch(name: str, args: dict, as_: str | None = None) -> str:
    """Route a tool call to its handler and return the serialized result.

    Gateway control ops dispatch through their ``Operation`` (the same handler
    that backs the HTTP route + CLI command): async ones are awaited on the
    server loop, sync ones run in a threadpool. Service ops are awaited over the
    service's control WS. See the module concurrency note. Raises ``ValueError``
    for an unknown tool (→ 404), :class:`ArgumentError` for an argument the verb
    does not declare (→ 400) and ``RuntimeError`` when a service's control
    channel is not open (→ 500), matching ``/invoke``'s exception→HTTP
    translation.

    Collapsed per-tool calls (the ``GET /tools?view=domains`` surface) arrive here
    too: a payload whose ``name`` is a known tool and whose ``args`` carries a
    ``verb`` is routed to :func:`_dispatch_domain` *ahead* of the flat branches.
    The ``"verb" in args`` guard keeps a future flat tool literally named like a
    tool from being hijacked, and the flat branches below stay intact so the CLI's
    by-name ``/invoke`` posts keep working.

    A tool **no local service provides** but a peer does routes here too, so a
    peer-only tool (``orch``, …) is callable by its plain name
    rather than needing a ``<tool>@<peer>`` twin.

    A peer node with no edge to this node (see :func:`awm.config.edges.admits`)
    is refused with ``Unknown tool``, the answer for a name that does not exist.

    A name that an earlier release published (see :mod:`aliases`) is rewritten to
    its successor first, so the gates below judge the verb that will run.
    """
    args = args or {}
    name, args = resolve_alias(name, args)
    if not edges.admits(as_):
        raise ValueError(f"Unknown tool: {name}")
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
        return await _run_entry({"op": op}, args, as_, unknown=f"Unknown tool: {name}")

    rec, fn = _find_service_fn(name)
    if rec is None or fn is None:
        raise ValueError(f"Unknown tool: {name}")
    spec = _spec_of(rec, fn)
    if spec is not None:
        usage.record(*_split_name(rec, spec))
    return await _run_entry({"rec": rec, "fn": fn}, args, as_,
                            unknown=f"Unknown tool: {name}")
