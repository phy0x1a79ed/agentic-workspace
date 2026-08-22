"""Which peer provides which MCP domain — the fleet-wide provider directory.

``peers.py`` is the address book (name → edge URL + ssh alias). This module is
the *capability* half: for each peer, which collapsed MCP domains it exposes and
what verbs each carries. Together they answer the only question the agent-facing
surface actually needs — "if I call ``2fa``, where does it land, and where else
could it?"

Why this exists at all
----------------------
The MCP surface is collapsed by domain precisely to stay small for clients that
cannot defer tool schemas. Federation used to undo that saving by merging every
peer's whole catalog under ``<domain>@<peer>`` names — 29 local domains became 82
tools on a two-peer node, growing by ~30 per peer joined, nearly all of it the
same domain re-listed per host. So *where a call runs* became a parameter of the
one tool (``peer``, beside ``verb``/``args``) instead of a reason to mint another,
and this module is what makes that parameter answerable.

Three design rules, each load-bearing:

**The snapshot is never on the request path.** A fetch costs an ssh (the bearer)
plus a TLS round trip to a host that may be asleep. ``refresh_loop`` sweeps in the
background under ``spawn_supervised`` and readers take whatever the last sweep
produced — stale on error, empty before the first. That is what lets ``GET
/tools`` stay fast and never hang on a sleeping peer, which the old inline
per-listing fetch could not promise.

**The peer fetch asks for the peer's LOCAL view** (plain ``?view=domains``, never
``peers=1``). A peer's own union would carry *its* peers, and our book may not
contain them — mira's book holds ``cosmos``, which this node cannot dial. We would
advertise providers we cannot reach. Peer chaining is deliberately not a thing.

**A declared singleton has exactly one provider.** Not "local plus the owner" —
one. Two nodes both treating themselves as a valid provider for a singular
external resource is the concrete failure FEDERATION.md documents: two listeners
spending one Duo attempt budget, two logins on one Discord token. Refusing the
off-target ``peer`` at the surface is the same "never half-route" rule the
``call_maybe_peer`` selectors enforce for service→service calls.

Which domains are singletons is **declared, never inferred from env-var shape.**
Of the three ``AWM_*_PEER`` selectors this node sets, only one means "the whole
domain lives elsewhere":

- ``AWM_TWOFA_PEER`` does — ``2fa`` is singular whole, and seeds the table.
- ``AWM_SOCIAL_PEER`` does **not** — ``social`` runs per-node (every node wants
  its own Slack, Gmail, buckets) and only individual *accounts* marked
  ``singleton = true`` are singular. Re-homing the domain would cost this node
  its own accounts.
- ``AWM_SSH_SLOT_PEER`` does **not** — only the connection-slot *arbiter role* is
  fleet-global, not the ``ssh`` service, which every node needs locally.

``AWM_DOMAIN_HOME_<domain>=<peer>`` declares a future singleton without a code
change. A node with no local provider for a domain that two peers both advertise
resolves it ``ambiguous`` and every call must name a peer by hand, so that escape
hatch is also the cure for *borrowing* a domain wholesale: altair sets
``AWM_DOMAIN_HOME_social=mira`` because it has no ``social.toml`` and therefore no
accounts to lose. The rule above is unchanged — the shape of ``AWM_SOCIAL_PEER``
still declares nothing; what differs is per-node fact about what is installed.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any, Mapping

import httpx

log = logging.getLogger("awm.gateway.peer_catalog")

# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------

#: Background sweep cadence. A peer's domain set changes only when a service is
#: added or a node is redeployed, so this is deliberately slow — the cost of
#: being two minutes stale is a tool description, not a failed call (dispatch
#: validates against the snapshot but the peer itself is the final authority).
_SWEEP_INTERVAL_S = 120.0

#: Per-peer ceiling for one catalog fetch. Bounded so a single blackholed peer
#: cannot stall the sweep for the others — they run concurrently, but a sweep
#: that never finishes never updates anything.
_PEER_FETCH_TIMEOUT_S = 8.0

#: Reserved provider name meaning "this node". Never a legal peer name (peers.add
#: rejects nothing of the sort, but a node named "local" would be ambiguous here,
#: so the name is claimed explicitly).
LOCAL = "local"

#: Reserved top-level MCP tool names that are not domains and must never be
#: treated as routable ones — including when read back off a peer's catalog.
RESERVED_TOOLS = frozenset({"providersOf"})

#: Seeded declared homes: domain → the env var naming its owning node. See the
#: module docstring for why this table has exactly one entry and must not be
#: generalised to every AWM_*_PEER selector.
_SEEDED_HOME_ENVS: dict[str, str] = {"2fa": "AWM_TWOFA_PEER"}

#: Generic escape hatch: AWM_DOMAIN_HOME_<domain>=<peer>.
_HOME_ENV_PREFIX = "AWM_DOMAIN_HOME_"


# ---------------------------------------------------------------------------
# Snapshot state — a plain module global, read sync, written only by the sweep.
# Same GIL-safe contract as the hub registry that ``list_tools`` reads.
# ---------------------------------------------------------------------------

#: peer name → {"domains": {domain: [verb, …]}, "reachable": bool,
#:              "error": str | None, "at": float}
_snapshot: dict[str, dict[str, Any]] = {}
_swept_at: float = 0.0
_sweep_lock: asyncio.Lock | None = None


def snapshot() -> dict[str, dict[str, Any]]:
    """The last sweep's result. Never blocks, never raises, possibly empty."""
    return _snapshot


def swept_at() -> float | None:
    """``time.time()`` of the last completed sweep, or ``None`` if never."""
    return _swept_at or None


def declared_homes() -> dict[str, str]:
    """Domain → owning peer, for domains declared to be whole-domain singletons.

    Read fresh on every call so a change to ``.awm/env`` takes effect on the next
    gateway restart without a code path caching it into permanence.
    """
    homes: dict[str, str] = {}
    for domain, env in _SEEDED_HOME_ENVS.items():
        owner = (os.environ.get(env) or "").strip()
        if owner:
            homes[domain] = owner
    for key, value in os.environ.items():
        if not key.startswith(_HOME_ENV_PREFIX):
            continue
        domain = key[len(_HOME_ENV_PREFIX):].strip()
        owner = (value or "").strip()
        if domain and owner:
            homes[domain] = owner
    return homes


# ---------------------------------------------------------------------------
# Fetching one peer's collapsed catalog
# ---------------------------------------------------------------------------


def _verbs_from_tool(tool: Mapping[str, Any]) -> list[str]:
    """Verb names for one collapsed domain tool from a peer's ``/tools`` payload.

    Prefers the structured ``inputSchema.properties.verb.enum``. Falls back to
    parsing the ``Verbs: a, b, c.`` clause out of the description, because a peer
    tracking an older ``release`` predates the enum — the fleet is updated node by
    node, so the mixed-version window is real and a peer must not read as
    verbless (which would make its domains look uncallable) during it.
    """
    schema = tool.get("inputSchema") or {}
    enum = ((schema.get("properties") or {}).get("verb") or {}).get("enum")
    if isinstance(enum, list) and enum:
        return [str(v) for v in enum]
    desc = tool.get("description") or ""
    marker = "Verbs:"
    if marker not in desc:
        return []
    tail = desc.split(marker, 1)[1]
    tail = tail.split(".", 1)[0]
    return [v.strip() for v in tail.split(",") if v.strip()]


async def _fetch_peer(entry: Mapping[str, Any]) -> dict[str, Any]:
    """One peer's ``{domain: [verbs]}`` map, read from its own edge.

    CA-verified TLS with a bearer fetched over ssh — the same path
    ``gatewayclient.call_peer`` uses, and never ``verify=False``: a bearer on an
    unverified connection could be captured. Raises on any failure; the sweep
    records that as ``reachable: False`` rather than dropping the peer, so
    "asleep" stays distinguishable from "doesn't have it".
    """
    from awm import gatewayclient

    name = entry["name"]
    edge = str(entry["edge_url"]).rstrip("/")
    alias = entry.get("ssh_alias") or name
    bearer = await gatewayclient.fetch_peer_cred_async(
        alias, timeout=_PEER_FETCH_TIMEOUT_S)
    ca = gatewayclient._peer_ca()
    async with httpx.AsyncClient(timeout=_PEER_FETCH_TIMEOUT_S, verify=ca) as cli:
        resp = await cli.get(f"{edge}/tools", params={"view": "domains"},
                             headers={"Authorization": f"Bearer {bearer}"})
        resp.raise_for_status()
        payload = resp.json() or {}
    domains: dict[str, list[str]] = {}
    for tool in payload.get("tools") or []:
        if not isinstance(tool, dict):
            continue
        tname = tool.get("name")
        # A peer's own reserved top-level tools (providersOf) are not domains and
        # must not become routable "domains" here.
        if not tname or not isinstance(tname, str) or tname in RESERVED_TOOLS:
            continue
        domains[tname] = _verbs_from_tool(tool)
    return domains


async def sweep() -> dict[str, dict[str, Any]]:
    """Refresh the snapshot from every peer in the address book, concurrently.

    Best-effort per peer and serialised against itself by a lock, so the
    background loop and an on-demand ``refresh`` can never interleave into a
    half-written snapshot. Returns the new snapshot.
    """
    global _snapshot, _swept_at, _sweep_lock

    if _sweep_lock is None:
        _sweep_lock = asyncio.Lock()
    async with _sweep_lock:
        from awm.gateway import peers

        book = peers.list_all()
        if not book:
            _snapshot, _swept_at = {}, time.time()
            return _snapshot
        results = await asyncio.gather(
            *(_fetch_peer(entry) for entry in book), return_exceptions=True)
        built: dict[str, dict[str, Any]] = {}
        now = time.time()
        for entry, result in zip(book, results):
            name = entry["name"]
            if isinstance(result, BaseException):
                # Keep the previous domain map so one failed sweep does not make
                # a peer's tools vanish and reappear; mark it unreachable.
                previous = (_snapshot.get(name) or {}).get("domains") or {}
                built[name] = {"domains": previous, "reachable": False,
                               "error": f"{type(result).__name__}: {result}",
                               "at": now}
                log.warning("peer %r catalog unavailable: %s", name, result)
            else:
                built[name] = {"domains": result, "reachable": True,
                               "error": None, "at": now}
        _snapshot, _swept_at = built, now
        return _snapshot


async def refresh_loop() -> None:
    """Sweep forever on ``_SWEEP_INTERVAL_S``. Started under ``spawn_supervised``.

    Never returns — the supervisor treats a return as a defect, so a failure has
    to be swallowed per-iteration rather than breaking out. ``sweep`` is already
    best-effort per peer; this catches anything structural (an unreadable address
    book) without taking the loop down.
    """
    while True:
        try:
            await sweep()
        except Exception:  # noqa: BLE001 — a sweep must never kill the loop
            log.error("peer catalog sweep failed", exc_info=True)
        await asyncio.sleep(_SWEEP_INTERVAL_S)


# ---------------------------------------------------------------------------
# Provider resolution — pure functions over (local domains, snapshot)
# ---------------------------------------------------------------------------
# Deliberately takes the local domain map as an argument rather than importing
# the catalog: ``catalog`` imports this module, so the dependency has to point
# one way only.

#: ``reason`` values on a resolution, i.e. why ``default`` is what it is.
REASON_SINGLETON = "declared-singleton"
REASON_LOCAL = "local"
REASON_SOLE_PEER = "sole-peer"
REASON_AMBIGUOUS = "ambiguous"
REASON_UNKNOWN = "unknown"


class PeerRedirect(Exception):
    """A call resolved to a peer: here is its address, dial it yourself.

    Raised out of dispatch instead of the gateway forwarding the call. The
    gateway is a *directory*, never a router — handing back an address is the
    same job ``peer_resolve`` already does, and it is what keeps the invariant
    that no peer bytes traverse a gateway. The MCP proxy declares it can follow
    one of these (it is the client-side federation point and already talks to
    peer edges); any other caller gets a plain error telling it to dial the edge,
    so nothing silently changes shape for the CLI or the web UI.
    """

    def __init__(self, peer: str, tool: str, verb: str | None = None) -> None:
        self.peer = peer
        self.tool = tool
        self.verb = verb
        super().__init__(
            f"{tool!r}{f'.{verb}' if verb else ''} is served by peer {peer!r}, "
            f"not this node")

    def payload(self) -> dict[str, Any]:
        """The address the caller needs, resolved from the local address book."""
        from awm.gateway import peers

        entry = peers.resolve(self.peer) or {}
        return {
            "peer": self.peer,
            "tool": self.tool,
            "verb": self.verb,
            "edge_url": entry.get("edge_url"),
            "ssh_alias": entry.get("ssh_alias") or self.peer,
        }


def _provider(name: str, verbs: list[str], reachable: bool,
              error: str | None) -> dict[str, Any]:
    return {"peer": name, "verbs": verbs, "reachable": reachable, "error": error}


def resolve(tool: str, local_domains: Mapping[str, list[str]],
            snap: Mapping[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    """Who can serve ``tool``, and which of them is the default.

    Returns ``{tool, default, reason, providers:[{peer, verbs, reachable,
    error}]}`` where ``default`` is ``"local"``, a peer name, or ``None``.
    ``local_domains`` maps this node's domain names to their verbs.

    The rules, in order:

    1. A **declared home** (a whole-domain singleton) has exactly that one
       provider, which is the default. Nothing else is valid — see the module
       docstring for why local is excluded rather than merely deprioritised.
    2. Otherwise a domain that exists **locally** defaults to local, with every
       peer that also has it available as an override.
    3. Otherwise, if exactly **one** peer has it, that peer is the sole provider
       and the default — so a peer-only domain is callable with no ``peer`` at
       all, which is what keeps today's ``orch``/``artifact`` reachable.
    4. Otherwise (several peers, no local, no declared home) there is **no
       default**: the caller must name one. Guessing would silently bind a call
       to whichever node happened to sort first.
    """
    snap = snapshot() if snap is None else snap

    home = declared_homes().get(tool)
    if home:
        info = snap.get(home) or {}
        verbs = (info.get("domains") or {}).get(tool) or []
        known = home in snap
        return {
            "tool": tool,
            "default": home,
            "reason": REASON_SINGLETON,
            "providers": [_provider(
                home, verbs, bool(info.get("reachable")),
                info.get("error") if known
                else f"peer {home!r} is not in this node's address book",
            )],
        }

    peer_providers = [
        _provider(name, (info.get("domains") or {}).get(tool) or [],
                  bool(info.get("reachable")), info.get("error"))
        for name, info in sorted(snap.items())
        if tool in (info.get("domains") or {})
    ]

    if tool in local_domains:
        providers = [_provider(LOCAL, list(local_domains[tool]), True, None)]
        providers.extend(peer_providers)
        return {"tool": tool, "default": LOCAL, "reason": REASON_LOCAL,
                "providers": providers}

    if len(peer_providers) == 1:
        return {"tool": tool, "default": peer_providers[0]["peer"],
                "reason": REASON_SOLE_PEER, "providers": peer_providers}

    if peer_providers:
        return {"tool": tool, "default": None, "reason": REASON_AMBIGUOUS,
                "providers": peer_providers}

    return {"tool": tool, "default": None, "reason": REASON_UNKNOWN,
            "providers": []}


def fleet_domains(local_domains: Mapping[str, list[str]],
                  snap: Mapping[str, dict[str, Any]] | None = None) -> list[str]:
    """Every domain name callable from this node — local ∪ every peer's.

    The union is what keeps the collapsed surface honest: a domain only a peer
    runs (``hpcllm``, ``orch``, …) still appears exactly once, so nothing that is
    reachable today stops being nameable. A declared singleton's domain is
    included even if this node runs no such service.
    """
    snap = snapshot() if snap is None else snap
    names = set(local_domains)
    for info in snap.values():
        names.update((info.get("domains") or {}).keys())
    names.update(declared_homes())
    return sorted(names)


def choose_target(tool: str, requested: str | None,
                  local_domains: Mapping[str, list[str]],
                  snap: Mapping[str, dict[str, Any]] | None = None) -> str:
    """The provider a call should land on: ``"local"`` or a peer name.

    ``requested`` is the caller's optional ``peer`` override (``"local"`` is
    legal and means "explicitly this node"). Raises ``ValueError`` naming the
    valid providers when the request cannot be honoured, or when there is no
    default to fall back on. It never quietly degrades to local: running a
    ``mira``-targeted call here is the half-route failure the whole
    default-provider model exists to prevent.
    """
    res = resolve(tool, local_domains, snap)
    valid = [p["peer"] for p in res["providers"]]

    if requested is None:
        if res["default"] is None:
            if res["reason"] == REASON_UNKNOWN:
                raise ValueError(f"Unknown tool: {tool}")
            raise ValueError(
                f"{tool!r} has no default provider — it runs on "
                f"{', '.join(valid)} but not on this node. Pass peer=<name>; "
                f"providersOf(tool={tool!r}) lists the options.")
        return res["default"]

    requested = requested.strip()
    if requested in valid:
        return requested
    if res["reason"] == REASON_SINGLETON:
        raise ValueError(
            f"{tool!r} is a singleton owned by {res['default']!r} — peer="
            f"{requested!r} is not a valid provider. Calling it anywhere else "
            f"would double-spend the single external resource it fronts.")
    if res["reason"] == REASON_UNKNOWN:
        raise ValueError(f"Unknown tool: {tool}")
    raise ValueError(
        f"peer={requested!r} does not provide {tool!r}. Valid providers: "
        f"{', '.join(valid)}.")
