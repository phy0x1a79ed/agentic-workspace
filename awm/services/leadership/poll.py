"""Periodic /status probe loop for leader election.

Every ``POLL_INTERVAL_S`` seconds, walk the peers with
``peer_priority < self.priority`` and call ``GET /status``. Apply the K=3 /
K=1 hysteresis from :mod:`awm.services.leadership.state`:

- If any higher-priority peer responded OK this round → STANDBY.
- Else, if all higher-priority peers have failed K=3 times in a row → ACTIVE.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from awm.services.leadership import state as ldr_state
from awm.services.network import peers as peer_svc

log = logging.getLogger("awm.leadership.poll")

POLL_INTERVAL_S = 3.0
PROBE_TIMEOUT_S = 2.5


async def _probe_one(peer: dict) -> tuple[str, bool, dict[str, Any] | None]:
    """Return (peer_id, ok, status_body)."""
    from awm.services.network import federation

    peer_id = peer["peer_id"]
    try:
        base_url, token = await asyncio.to_thread(federation._resolve, peer_id)
    except Exception as exc:  # noqa: BLE001
        log.debug("probe resolve failed for %s: %s", peer_id, exc)
        return (peer_id, False, None)

    headers: dict[str, str] = {"Authorization": f"Bearer {token}"}
    try:
        self_id = ldr_state.get_state().self_peer_id
        if self_id:
            headers["X-Awm-From"] = self_id
    except Exception:
        pass

    url = base_url.rstrip("/") + "/status"
    try:
        async with httpx.AsyncClient(verify=False, timeout=PROBE_TIMEOUT_S) as client:
            r = await client.get(url, headers=headers)
    except Exception as exc:  # noqa: BLE001
        log.debug("probe %s -> network error: %s", peer_id, exc)
        return (peer_id, False, None)
    if r.status_code != 200:
        log.debug("probe %s -> HTTP %d", peer_id, r.status_code)
        return (peer_id, False, None)
    try:
        body = r.json()
    except Exception:
        return (peer_id, True, None)
    return (peer_id, True, body)


async def evaluate_once() -> None:
    """Single probe round: probe every higher-priority peer, apply state
    transitions per the K thresholds."""
    s = ldr_state.get_state()
    if s.self_peer_id is None:
        return

    higher = [p for p in peer_svc.list_remote_peers()
              if int(p.get("peer_priority", 100)) < int(s.self_priority)]

    if not higher:
        # No one outranks us — we are by definition the leader.
        await ldr_state.promote(s.self_peer_id)
        return

    results = await asyncio.gather(*[_probe_one(p) for p in higher])
    any_ok = False
    best_leader: str | None = None
    best_leader_priority: int | None = None
    for (peer_id, ok, body) in results:
        ldr_state.record_probe_result(peer_id, ok)
        if ok:
            any_ok = True
            # Track the lowest-priority responsive peer as the leader.
            priority = next(
                (int(p.get("peer_priority", 100)) for p in higher if p["peer_id"] == peer_id),
                100,
            )
            if best_leader_priority is None or priority < best_leader_priority:
                best_leader = peer_id
                best_leader_priority = priority

    if any_ok:
        # A higher-priority peer is up — yield (or stay STANDBY).
        await ldr_state.demote(best_leader)
        return

    # All higher-priority peers failed this round. Promote only if every
    # one of them has crossed the K=3 failure threshold.
    all_at_threshold = all(
        not ldr_state.has_recent_ok(p["peer_id"]) for p in higher
    )
    if all_at_threshold:
        await ldr_state.promote(s.self_peer_id)
    else:
        # Still in cooldown — keep current_leader as the most recently OK
        # higher peer if we know one, else None.
        with s._lock:
            if s.leadership != "ACTIVE" and s.current_leader is None:
                # First few rounds — don't claim leadership yet; redirect
                # would have nowhere to go, so /ui hits get raw 503.
                pass


async def run_poll_loop(stop_event: asyncio.Event | None = None) -> None:
    """Long-running task: probe every POLL_INTERVAL_S until cancelled or
    ``stop_event`` is set."""
    log.info("leadership poll loop starting (interval=%.1fs)", POLL_INTERVAL_S)
    while True:
        try:
            await evaluate_once()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("evaluate_once raised: %s", exc)
        try:
            if stop_event is not None:
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=POLL_INTERVAL_S)
                    return
                except asyncio.TimeoutError:
                    continue
            else:
                await asyncio.sleep(POLL_INTERVAL_S)
        except asyncio.CancelledError:
            raise
