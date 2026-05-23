"""Leadership state singleton.

Holds the local peer's STANDBY/ACTIVE state, per-peer probe bookkeeping, and
the callback list invoked on transitions. Pure data + callback dispatch — the
probe loop in :mod:`awm.services.leadership.poll` mutates this state; the
gating middleware in :mod:`awm.exposed` reads it.

Transitions are intentionally asymmetric: K=3 consecutive failures to promote
(absorbs ZeroTier flap), K=1 success to demote (lower-priority always yields
the moment its better lands).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from threading import RLock
from typing import Awaitable, Callable

log = logging.getLogger("awm.leadership")

PROMOTE_FAIL_THRESHOLD = 3   # consecutive /status failures from a higher-priority peer before promoting


Callback = Callable[[], Awaitable[None] | None]


@dataclass
class State:
    """Module-level singleton (see :func:`get_state`)."""

    leadership: str = "STANDBY"           # "ACTIVE" | "STANDBY"
    self_peer_id: str | None = None
    self_priority: int = 100
    current_leader: str | None = None     # peer_id self thinks is leader
    last_transition_at: str | None = None

    # Per-peer probe bookkeeping (peer_id -> int / datetime)
    consecutive_fail: dict[str, int] = field(default_factory=dict)
    last_ok: dict[str, datetime] = field(default_factory=dict)

    # Transition callbacks. Sync or async; awaited if coroutine.
    _on_promote: list[Callback] = field(default_factory=list)
    _on_demote: list[Callback] = field(default_factory=list)

    _lock: RLock = field(default_factory=RLock, repr=False)


_STATE: State | None = None


def get_state() -> State:
    """Return the process-singleton state, initializing it on first call."""
    global _STATE
    if _STATE is None:
        _STATE = State()
    return _STATE


def reset_for_test() -> None:
    """Drop the singleton so tests can start clean."""
    global _STATE
    _STATE = None


def configure(self_peer_id: str, self_priority: int) -> None:
    """Seed the state with this host's identity. Called from lifespan after
    init_db() so the peers-table self-row exists."""
    s = get_state()
    with s._lock:
        s.self_peer_id = self_peer_id
        s.self_priority = self_priority
        # Start in STANDBY by default — the first poll round decides.
        s.leadership = "STANDBY"
        s.current_leader = None


def on_promote(cb: Callback) -> None:
    """Register a callback to invoke on STANDBY→ACTIVE."""
    get_state()._on_promote.append(cb)


def on_demote(cb: Callback) -> None:
    """Register a callback to invoke on ACTIVE→STANDBY."""
    get_state()._on_demote.append(cb)


async def _fire(callbacks: list[Callback]) -> None:
    import inspect
    for cb in list(callbacks):
        try:
            result = cb()
            if inspect.isawaitable(result):
                await result
        except Exception as exc:  # noqa: BLE001
            log.warning("leadership callback %r failed: %s", cb, exc)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def promote(leader_peer_id: str | None = None) -> None:
    """Transition STANDBY→ACTIVE if not already. ``leader_peer_id`` defaults
    to self."""
    s = get_state()
    fire = False
    with s._lock:
        if s.leadership != "ACTIVE":
            s.leadership = "ACTIVE"
            s.current_leader = leader_peer_id or s.self_peer_id
            s.last_transition_at = _now_iso()
            fire = True
            log.info("leadership: STANDBY → ACTIVE (self=%s)", s.self_peer_id)
        else:
            s.current_leader = leader_peer_id or s.self_peer_id
    if fire:
        await _fire(s._on_promote)
        _write_discovery()


async def demote(new_leader_peer_id: str | None) -> None:
    """Transition ACTIVE→STANDBY (or update current_leader while already
    STANDBY)."""
    s = get_state()
    fire = False
    with s._lock:
        if s.leadership != "STANDBY":
            s.leadership = "STANDBY"
            s.last_transition_at = _now_iso()
            fire = True
            log.info(
                "leadership: ACTIVE → STANDBY (yielding to %s)", new_leader_peer_id
            )
        s.current_leader = new_leader_peer_id
    if fire:
        await _fire(s._on_demote)
    _write_discovery()


def record_probe_result(peer_id: str, ok: bool) -> None:
    """Update per-peer success/failure counters. Returns nothing; the poll
    loop uses :func:`evaluate` to decide if a transition should happen."""
    s = get_state()
    with s._lock:
        if ok:
            s.consecutive_fail[peer_id] = 0
            s.last_ok[peer_id] = datetime.now(timezone.utc)
        else:
            s.consecutive_fail[peer_id] = s.consecutive_fail.get(peer_id, 0) + 1


def has_recent_ok(peer_id: str) -> bool:
    """True iff the last probe result for this peer was a success (failures
    not yet at threshold count as "still up" — we only flip on K=3 fails)."""
    s = get_state()
    with s._lock:
        return s.consecutive_fail.get(peer_id, 0) < PROMOTE_FAIL_THRESHOLD


def current_leader_base_url() -> str | None:
    """Return the base URL of the current leader's first direct endpoint,
    for 503 Location redirects. None when leader is unknown or self."""
    s = get_state()
    with s._lock:
        leader = s.current_leader
    if not leader or leader == s.self_peer_id:
        return None
    from awm.services.network import peers as peer_svc
    p = peer_svc.get_peer(leader)
    if not p:
        return None
    for ep in p.get("endpoints") or []:
        if ep.get("kind") == "direct" and ep.get("url"):
            return ep["url"].rstrip("/")
    return None


def _write_discovery() -> None:
    """Refresh ``$AWM_DIR/exposed.json`` so external probes (CLI, MCP proxy)
    see the current leadership view without polling the running process."""
    try:
        from awm.exposed import _write_discovery_file
        _write_discovery_file()
    except Exception:
        # Best-effort: discovery file is a hint, not a contract.
        pass
