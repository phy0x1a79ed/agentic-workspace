"""Pull-based cr-sqlite replication loop.

Every ``SYNC_INTERVAL_S`` seconds, ask each remote peer for changes since
the last db_version we have from them. Apply locally via
:func:`endpoint.apply_changes`. Per-peer cursor lives in the
``peer_sync_state`` table (local-only, not replicated).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

import httpx

from awm.db import get_connection
from awm.services.replication import endpoint as repl_endpoint
from awm.services.replication import schema as repl_schema

log = logging.getLogger("awm.replication.sync")

SYNC_INTERVAL_S = 5.0
SYNC_TIMEOUT_S = 10.0


def get_cursor(peer_id: str) -> int:
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT last_db_version FROM peer_sync_state WHERE peer_id = ?",
            (peer_id,),
        ).fetchone()
        return int(row["last_db_version"]) if row else 0
    finally:
        repl_schema.finalize(conn)
        conn.close()


def set_cursor(peer_id: str, db_version: int) -> None:
    now = datetime.now(timezone.utc).isoformat()
    conn = get_connection()
    try:
        conn.execute(
            """
            INSERT INTO peer_sync_state (peer_id, last_db_version, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(peer_id) DO UPDATE SET
                last_db_version = excluded.last_db_version,
                updated_at = excluded.updated_at
            """,
            (peer_id, db_version, now),
        )
        conn.commit()
    finally:
        repl_schema.finalize(conn)
        conn.close()


async def pull_from(peer_id: str) -> int:
    """Pull one batch of changes from ``peer_id``. Returns rows applied."""
    from awm.services.network import federation
    from awm.services.leadership import state as ldr_state

    try:
        base_url, token = await asyncio.to_thread(federation._resolve, peer_id)
    except Exception as exc:  # noqa: BLE001
        log.debug("pull resolve failed for %s: %s", peer_id, exc)
        return 0

    headers = {"Authorization": f"Bearer {token}"}
    self_id = ldr_state.get_state().self_peer_id
    if self_id:
        headers["X-Awm-From"] = self_id

    since = get_cursor(peer_id)
    url = base_url.rstrip("/") + f"/peer/db-sync?since={since}"
    try:
        async with httpx.AsyncClient(verify=False, timeout=SYNC_TIMEOUT_S) as client:
            r = await client.get(url, headers=headers)
    except Exception as exc:  # noqa: BLE001
        log.debug("pull %s -> network error: %s", peer_id, exc)
        return 0
    if r.status_code == 503:
        # Peer doesn't have cr-sqlite loaded — that's fine, nothing to do.
        return 0
    if r.status_code != 200:
        log.debug("pull %s -> HTTP %d: %s", peer_id, r.status_code, r.text[:200])
        return 0
    try:
        body = r.json()
    except Exception:
        log.debug("pull %s -> non-JSON response", peer_id)
        return 0
    changes = body.get("changes") or []
    if not changes:
        return 0

    def _apply() -> int:
        conn = get_connection()
        try:
            return repl_endpoint.apply_changes(conn, changes)
        finally:
            repl_schema.finalize(conn)
            conn.close()

    applied = await asyncio.to_thread(_apply)
    new_cursor = int(body.get("db_version", since))
    if new_cursor > since:
        set_cursor(peer_id, new_cursor)
    log.info("pull %s: applied %d rows (cursor %d → %d)",
             peer_id, applied, since, new_cursor)
    return applied


async def evaluate_once() -> int:
    """Pull from every remote peer once. Returns total rows applied."""
    if not repl_schema.crsqlite_available():
        return 0
    from awm.services.network import peers as peer_svc
    total = 0
    for p in peer_svc.list_remote_peers():
        try:
            total += await pull_from(p["peer_id"])
        except Exception as exc:  # noqa: BLE001
            log.warning("pull_from(%s) raised: %s", p["peer_id"], exc)
    return total


async def run_sync_loop(stop_event: asyncio.Event | None = None) -> None:
    log.info("replication sync loop starting (interval=%.1fs)", SYNC_INTERVAL_S)
    while True:
        try:
            await evaluate_once()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("sync evaluate_once raised: %s", exc)
        try:
            if stop_event is not None:
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=SYNC_INTERVAL_S)
                    return
                except asyncio.TimeoutError:
                    continue
            else:
                await asyncio.sleep(SYNC_INTERVAL_S)
        except asyncio.CancelledError:
            raise
