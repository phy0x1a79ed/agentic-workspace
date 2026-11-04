"""Which peer provides which MCP domain — the fleet-wide provider directory.

For each node this one holds an edge to (``awm.config.edges``), which collapsed
MCP domains it exposes and what verbs each carries. It answers the only question
the agent-facing surface actually needs — "if I call ``2fa``, where does it land,
and where else could it?" This Python gateway reads no peer's catalog: only the
Rust gateway reaches a peer, over the ssh edge, so every peer here stays
unreachable until T19 moves the node onto it.

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

**Only peers this node holds an edge to are providers** (``awm.config.edges``).
The sweep reads a book peer only when this node reaches it, and every reader
(``snapshot``, ``resolve``, ``fleet_domains``, ``declared_homes``) keeps only such
peers on read as well, so a peer whose edge is removed between sweeps vanishes at
once and an unreadable book leaves no provider at all. A peer with no edge from
here never reaches a tool description, or a default provider.

**A declared singleton has exactly one provider.** Not "local plus the owner" —
one. Two nodes both treating themselves as a valid provider for a singular
external resource is the concrete failure FEDERATION.md documents: two listeners
spending one Duo attempt budget, two logins on one Discord token. Refusing the
off-target ``peer`` at the surface is the same "never half-route" rule the
``peer_env`` selectors enforce for service→service calls.

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


log = logging.getLogger("awm.gateway.peer_catalog")

# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------

#: Background sweep cadence. A peer's domain set changes only when a service is
#: added or a node is redeployed, so this is deliberately slow — the cost of
#: being two minutes stale is a tool description, not a failed call (dispatch
#: validates against the snapshot but the peer itself is the final authority).
_SWEEP_INTERVAL_S = 120.0

#: Faster cadence while a peer has never answered, for the first minutes after
#: start. At boot the first sweep runs before the auth service can sign a node
#: token, and waiting the full interval leaves every session without the fleet's
#: tools for two minutes.
_SWEEP_RETRY_S = 10.0
_SWEEP_RETRY_FOR_S = 300.0

#: Reserved provider name meaning "this node". A node named "local" would be
#: ambiguous here, so the name is claimed explicitly.
LOCAL = "local"

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
#: Why no peer's catalog is read here.
NEEDS_RUST_GATEWAY = "reaching a peer needs the Rust gateway (T19), which carries calls over the ssh edge"


def _edge_peers() -> frozenset[str]:
    """The nodes this node holds an edge to, read from the edge list on every call."""
    from awm.config import edges

    return frozenset(edges.reachable_from(edges.here()))


def _edge_only(snap: Mapping[str, dict[str, Any]]) -> Mapping[str, dict[str, Any]]:
    """``snap`` cut to the peers this node holds an edge to."""
    reachable = _edge_peers()
    if all(name in reachable for name in snap):
        return snap
    return {name: info for name, info in snap.items() if name in reachable}


def snapshot() -> dict[str, dict[str, Any]]:
    """The last sweep's result, edge peers only. Never blocks, never raises,
    possibly empty."""
    return dict(_edge_only(_snapshot))


def swept_at() -> float | None:
    """``time.time()`` of the last completed sweep, or ``None`` if never."""
    return _swept_at or None


def declared_homes() -> dict[str, str]:
    """Domain → owning peer, for domains declared to be whole-domain singletons.

    Read fresh on every call so a change to ``.awm/env`` takes effect on the next
    gateway restart without a code path caching it into permanence. A home owned
    by a peer this node has no edge to is dropped: such a peer is never a provider.
    """
    reachable = _edge_peers()
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
    return {d: o for d, o in homes.items() if o in reachable}


async def sweep() -> dict[str, dict[str, Any]]:
    """Mark every node this one holds an edge to as unreachable from here.

    Only the Rust gateway reads a peer's catalog, over the ssh edge. Keeping the
    peers listed, with the reason, lets ``awm providers`` say why none serves.
    """
    global _snapshot, _swept_at
    now = time.time()
    _snapshot = {name: {"domains": {}, "descriptions": {}, "reachable": False,
                        "error": NEEDS_RUST_GATEWAY, "at": now}
                 for name in sorted(_edge_peers())}
    _swept_at = now
    return _snapshot


async def refresh_loop() -> None:
    """Sweep forever on ``_SWEEP_INTERVAL_S``. Started under ``spawn_supervised``.

    Never returns — the supervisor treats a return as a defect, so a failure has
    to be swallowed per-iteration rather than breaking out. ``sweep`` is already
    best-effort per peer; this catches anything structural (an unreadable address
    book) without taking the loop down.
    """
    started = time.monotonic()
    while True:
        try:
            await sweep()
        except Exception:  # noqa: BLE001 — a sweep must never kill the loop
            log.error("peer catalog sweep failed", exc_info=True)
        await asyncio.sleep(next_sweep_in(time.monotonic() - started))


#: A call that finds a peer's catalog unread sweeps on demand, unless a sweep
#: finished this recently: a burst of calls in the warm-up window must not queue
#: one sweep each behind the lock.
_ON_DEMAND_MIN_AGE_S = 2.0

_loaded_at = time.monotonic()


def warming_up() -> bool:
    """Whether this gateway started recently enough that an unread peer
    probably reflects the boot race rather than a peer that is away."""
    return time.monotonic() - _loaded_at < _SWEEP_RETRY_FOR_S


def catalog_pending(peer: str,
                    snap: Mapping[str, dict[str, Any]] | None = None) -> str | None:
    """Why this node does not know ``peer``'s domains yet, or ``None`` once it does.

    An edge peer no sweep has read has no domain map, so ``resolve`` cannot
    list it as a provider of anything. That is "not loaded", not "does not
    provide". It happens in the seconds after a gateway restart: the first sweep
    runs before the auth service can sign a node token, and the next one comes
    ``_SWEEP_RETRY_S`` later. A peer read once keeps its last map when it fails
    later, so only a peer never read is pending.
    """
    if peer not in _edge_peers():
        return None
    snap = snapshot() if snap is None else snap
    info = snap.get(peer)
    if info is None:
        return "no catalog sweep has read it yet"
    if info.get("domains") or info.get("reachable"):
        return None
    return info.get("error") or "no catalog sweep has read it yet"


async def load_pending(peer: str) -> str | None:
    """Sweep now if ``peer``'s catalog is pending, and say whether it still is."""
    if catalog_pending(peer) is None:
        return None
    if time.time() - _swept_at >= _ON_DEMAND_MIN_AGE_S:
        await sweep()
    return catalog_pending(peer)


def next_sweep_in(elapsed: float) -> float:
    never_reached = any(not p.get("reachable") and not p.get("domains")
                        for p in _snapshot.values())
    if never_reached and elapsed < _SWEEP_RETRY_FOR_S:
        return _SWEEP_RETRY_S
    return _SWEEP_INTERVAL_S


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
    """A call resolved to a peer, which this gateway cannot reach.

    Raised out of dispatch instead of running the call here. Only the Rust
    gateway carries a call to a peer, over the ssh edge (fleet.md R92, R93).
    """

    def __init__(self, peer: str, tool: str, verb: str | None = None) -> None:
        self.peer = peer
        self.tool = tool
        self.verb = verb
        super().__init__(
            f"{tool!r}{f'.{verb}' if verb else ''} is served by peer {peer!r}, "
            f"not this node")


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
       all, which is what keeps today's ``orch`` reachable.
    4. Otherwise (several peers, no local, no declared home) there is **no
       default**: the caller must name one. Guessing would silently bind a call
       to whichever node happened to sort first.
    """
    snap = snapshot() if snap is None else _edge_only(snap)

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


def description_for(res: Mapping[str, Any],
                    snap: Mapping[str, dict[str, Any]] | None = None) -> str | None:
    """The tool description the default provider of ``res`` published, or
    ``None`` when this node serves it itself or the peer sent none. A tool only a
    peer runs keeps its description on this node through it."""
    default = res.get("default")
    if default in (None, LOCAL):
        return None
    snap = snapshot() if snap is None else _edge_only(snap)
    text = ((snap.get(default) or {}).get("descriptions") or {}).get(res["tool"])
    return text if isinstance(text, str) and text.strip() else None


def fleet_domains(local_domains: Mapping[str, list[str]],
                  snap: Mapping[str, dict[str, Any]] | None = None) -> list[str]:
    """Every domain name callable from this node — local ∪ every peer's.

    The union is what keeps the collapsed surface honest: a domain only a peer
    runs (``orch``, …) still appears exactly once, so nothing that is
    reachable today stops being nameable. A declared singleton's domain is
    included even if this node runs no such service.
    """
    snap = snapshot() if snap is None else _edge_only(snap)
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
                f"{', '.join(valid)} but not on this node. Pass peer=<name>.")
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
