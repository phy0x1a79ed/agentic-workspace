"""Durable state: tunable thresholds, escape-hatch grants, and the ledger.

Three tables in the service's own SQLite DB (``awm.persistence`` gives every
service its own; there is no shared ``state.db``):

``settings``
    Runtime-tunable knobs, so raising a threshold or flipping dry-run does not
    need an edit-and-restart. Anything absent falls back to the derived default
    in :mod:`awm.compute.policy`.
``grants``
    An agent that legitimately needs more of the box than the cap allows can
    take a bounded, reasoned, logged exemption rather than being told no. The
    bound is the point: a grant has a size and an expiry, and cannot be open-
    ended.
``decisions``
    Every judgement, including the ones that were dropped and the ones dry-run
    suppressed. A watchdog you cannot audit is one you will end up disabling,
    so the record of "considered and declined" matters as much as the record of
    "acted".
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any

from awm.persistence.databases import get_connection, init_service_db

SERVICE = "compute"
SCHEMA_VERSION = 1

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS grants (
    id         TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    cpu_cores  REAL,
    mem_gb     REAL,
    reason     TEXT NOT NULL,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    revoked_at REAL
);
CREATE INDEX IF NOT EXISTS idx_grants_session ON grants(session_id, expires_at);

CREATE TABLE IF NOT EXISTS decisions (
    id         TEXT PRIMARY KEY,
    ts         REAL NOT NULL,
    session_id TEXT NOT NULL,
    metric     TEXT NOT NULL,
    kind       TEXT NOT NULL,
    measured   REAL NOT NULL,
    cap        REAL NOT NULL,
    action     TEXT NOT NULL,
    outcome    TEXT NOT NULL,
    target_pid INTEGER,
    cmdline    TEXT,
    detail     TEXT
);
CREATE INDEX IF NOT EXISTS idx_decisions_ts ON decisions(ts DESC);
"""


def init() -> None:
    init_service_db(SERVICE, SCHEMA_SQL, schema_version=SCHEMA_VERSION)


# -- settings ---------------------------------------------------------------


def get_settings() -> dict[str, Any]:
    conn = get_connection(SERVICE)
    try:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
    finally:
        conn.close()
    out: dict[str, Any] = {}
    for row in rows:
        try:
            out[row["key"]] = json.loads(row["value"])
        except json.JSONDecodeError:
            out[row["key"]] = row["value"]
    return out


def clear_settings(keys: list[str]) -> int:
    """Drop overrides so the derived-from-box-size defaults apply again."""
    if not keys:
        return 0
    conn = get_connection(SERVICE)
    try:
        cur = conn.execute(
            f"DELETE FROM settings WHERE key IN ({','.join('?' * len(keys))})",
            keys,
        )
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def set_settings(values: dict[str, Any]) -> None:
    conn = get_connection(SERVICE)
    try:
        conn.executemany(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            [(k, json.dumps(v)) for k, v in values.items()],
        )
        conn.commit()
    finally:
        conn.close()


# -- grants -----------------------------------------------------------------


def add_grant(
    session_id: str,
    *,
    reason: str,
    cpu_cores: float | None,
    mem_gb: float | None,
    ttl_s: float,
) -> dict[str, Any]:
    now = time.time()
    row = {
        "id": uuid.uuid4().hex,
        "session_id": session_id,
        "cpu_cores": cpu_cores,
        "mem_gb": mem_gb,
        "reason": reason,
        "created_at": now,
        "expires_at": now + ttl_s,
        "revoked_at": None,
    }
    conn = get_connection(SERVICE)
    try:
        conn.execute(
            "INSERT INTO grants (id, session_id, cpu_cores, mem_gb, reason, "
            "created_at, expires_at, revoked_at) VALUES (:id, :session_id, "
            ":cpu_cores, :mem_gb, :reason, :created_at, :expires_at, :revoked_at)",
            row,
        )
        conn.commit()
    finally:
        conn.close()
    return row


def revoke_grants(session_id: str) -> int:
    conn = get_connection(SERVICE)
    try:
        cur = conn.execute(
            "UPDATE grants SET revoked_at = ? WHERE session_id = ? "
            "AND revoked_at IS NULL AND expires_at > ?",
            (time.time(), session_id, time.time()),
        )
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def active_grants() -> dict[str, dict[str, Any]]:
    """Live grants keyed by session. Latest wins if a session has several."""
    now = time.time()
    conn = get_connection(SERVICE)
    try:
        rows = conn.execute(
            "SELECT * FROM grants WHERE revoked_at IS NULL AND expires_at > ? "
            "ORDER BY created_at",
            (now,),
        ).fetchall()
    finally:
        conn.close()
    return {r["session_id"]: dict(r) for r in rows}


# -- decisions --------------------------------------------------------------


def record_decision(
    *,
    session_id: str,
    metric: str,
    kind: str,
    measured: float,
    cap: float,
    action: str,
    outcome: str,
    target_pid: int | None = None,
    cmdline: str | None = None,
    detail: dict | None = None,
) -> str:
    did = uuid.uuid4().hex
    conn = get_connection(SERVICE)
    try:
        conn.execute(
            "INSERT INTO decisions (id, ts, session_id, metric, kind, measured, "
            "cap, action, outcome, target_pid, cmdline, detail) VALUES "
            "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (did, time.time(), session_id, metric, kind, measured, cap, action,
             outcome, target_pid, cmdline, json.dumps(detail or {})),
        )
        conn.commit()
    finally:
        conn.close()
    return did


def recent_decisions(limit: int = 50) -> list[dict[str, Any]]:
    conn = get_connection(SERVICE)
    try:
        rows = conn.execute(
            "SELECT * FROM decisions ORDER BY ts DESC LIMIT ?", (limit,)
        ).fetchall()
    finally:
        conn.close()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["detail"] = json.loads(d.get("detail") or "{}")
        except json.JSONDecodeError:
            d["detail"] = {}
        out.append(d)
    return out
