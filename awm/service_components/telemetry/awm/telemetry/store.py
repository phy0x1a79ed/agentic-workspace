"""Append-only event store — the durable side of a telemetry stream.

A *telemetry stream* is an ordered, idempotent sequence of events a service
publishes for a third-party client to sync (by poll or by live stream). The
store lives in the HOST service's OWN SQLite DB as one extra table, so a service
adopts telemetry without standing up a second database. It owns its table via a
plain ``CREATE TABLE IF NOT EXISTS`` (additive — it does NOT touch the host's
``schema_version`` machinery).

The cursor convention — the one rule, mirrored server + client:

- ``seq`` is the monotonic ordering key (the table rowid via ``AUTOINCREMENT``:
  strictly increasing, never reused). Poll and live stream agree on it.
- ``id`` is a stable uuid the client dedupes on across the backfill/live
  overlap (the same dedupe key end-to-end).

A read is "everything after ``seq``"; the ``id`` is the client's idempotency
tiebreak, not a server-side filter (``seq`` alone is unambiguous).
"""

from __future__ import annotations

import json
import time
import uuid

from awm.persistence.dao import BaseDAO
from awm.persistence.databases import get_connection, service_db_path

_SCHEMA = """\
CREATE TABLE IF NOT EXISTS telemetry_events (
    seq     INTEGER PRIMARY KEY AUTOINCREMENT,
    id      TEXT NOT NULL UNIQUE,
    stream  TEXT NOT NULL,
    kind    TEXT NOT NULL,
    body    TEXT NOT NULL DEFAULT '',
    meta    TEXT NOT NULL DEFAULT '{}',
    ts      INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_telemetry_events_stream_seq
    ON telemetry_events(stream, seq);
"""

# Per-DB-path guard so the IF-NOT-EXISTS DDL runs once per database file, not on
# every store construction. Keyed by the resolved path (not the service name) so
# a fresh DB under a different AWM_DIR — e.g. a test temp dir — re-ensures.
_ensured: set[str] = set()


def _now_ms() -> int:
    return int(time.time() * 1000)


def _event_from_row(row: dict) -> dict:
    """Shape one ``telemetry_events`` row into the wire event."""
    try:
        meta = json.loads(row.get("meta") or "{}")
    except (TypeError, ValueError):
        meta = {}
    return {
        "seq": row["seq"],
        "id": row["id"],
        "stream": row["stream"],
        "kind": row["kind"],
        "body": row.get("body") or "",
        "meta": meta,
        "ts": row["ts"],
    }


class TelemetryStore(BaseDAO):
    """Append-only telemetry events in a host service's own DB.

    ``TelemetryStore("orchestrator")`` ensures the ``telemetry_events`` table in
    the orchestrator's DB and reads/writes it via the shared :class:`BaseDAO`
    helpers. One store per service; cheap to construct.
    """

    def __init__(self, service: str, conn=None) -> None:
        super().__init__(service, conn=conn)
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        key = str(service_db_path(self.service))
        if key in _ensured:
            return
        conn = get_connection(self.service)
        try:
            conn.executescript(_SCHEMA)
            conn.commit()
        finally:
            conn.close()
        _ensured.add(key)

    def append(self, stream: str, kind: str, body: str = "",
               meta: dict | None = None, *,
               ts: int | None = None, id: str | None = None) -> dict:
        """Append one event to ``stream``; return the durable wire event.

        Mints a uuid ``id`` (unless supplied) and stamps ``ts``; ``seq`` is the
        assigned rowid. The returned ``{seq, id, stream, kind, body, meta, ts}``
        is exactly what the bus publishes and the client receives."""
        ev_id = id or uuid.uuid4().hex
        ev_ts = _now_ms() if ts is None else int(ts)
        body_s = "" if body is None else str(body)
        seq = self.execute(
            "INSERT INTO telemetry_events (id, stream, kind, body, meta, ts) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (ev_id, stream, kind, body_s, json.dumps(meta or {}), ev_ts),
        )
        return {"seq": seq, "id": ev_id, "stream": stream, "kind": kind,
                "body": body_s, "meta": meta or {}, "ts": ev_ts}

    def read_after(self, stream: str, after_seq: int | None = None, *,
                   after_id: str | None = None,
                   limit: int | None = None) -> list[dict]:
        """Events on ``stream`` strictly after ``after_seq``, ordered by ``seq``.

        ``after_seq=None`` returns the whole stream (the connect-time backfill).
        ``after_id`` is accepted for cursor symmetry with the client but not
        used as a filter — ``seq`` is a complete, unambiguous cursor."""
        sql = "SELECT * FROM telemetry_events WHERE stream=?"
        params: list = [stream]
        if after_seq is not None:
            sql += " AND seq > ?"
            params.append(int(after_seq))
        sql += " ORDER BY seq"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(int(limit))
        return [_event_from_row(r) for r in self.query_all(sql, params)]
