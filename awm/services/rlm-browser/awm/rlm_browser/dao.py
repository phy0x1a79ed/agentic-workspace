"""rlm-browser service data access — the ``rlm_sessions`` table on the service's
OWN SQLite DB (``AWM_DIR/services/rlm-browser/rlm-browser.db``).

Per the modular invariant there is no shared ``state.db``: this service owns its
tables and stands them up via ``init_service_db`` at startup. Each row is one
realm session — a Chrome process reachable over CDP on ``127.0.0.1:<cdp_port>``,
bound to a single game.

What the row persists is the bare minimum needed to *reconnect* to a session's
Chrome after a service restart wipes the in-process manager cache: the CDP port,
the on-disk profile dir, the launch ``mode`` (headless/rendered/attached), and
the backend that owns the process. Tabs are NOT persisted — they are live CDP *targets*,
enumerated by reading the running Chrome's ``/json`` on demand. ``egress`` is the
inert VPN seam (T4): accepted at acquire, echoed in status, never acted on here.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone

from awm.persistence.dao import BaseDAO
from awm.persistence.databases import init_service_db

SERVICE = "rlm-browser"
# v2: real-browser columns (mode/cdp_port/user_data_dir/egress/backend +
# docker's container_name/vnc_port). v1 was the skeleton's bare session row.
SCHEMA_VERSION = 2

SCHEMA_SQL = """\
CREATE TABLE IF NOT EXISTS rlm_sessions (
    session_id      TEXT NOT NULL PRIMARY KEY,
    game            TEXT NOT NULL DEFAULT '',
    status          TEXT NOT NULL DEFAULT 'active',
    -- launch shape, fixed for the session's life
    mode            TEXT NOT NULL DEFAULT 'headless', -- headless | rendered | attached
    backend         TEXT NOT NULL DEFAULT 'host',     -- host | docker | attached
    -- how to reconnect to this session's Chrome over CDP
    cdp_port        INTEGER NOT NULL DEFAULT 0,
    user_data_dir   TEXT NOT NULL DEFAULT '',
    -- docker backend only (NULL/empty for host)
    container_name  TEXT NOT NULL DEFAULT '',
    vnc_port        INTEGER NOT NULL DEFAULT 0,
    -- inert VPN seam (T4): JSON descriptor, never dialed here
    egress          TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL DEFAULT '',
    updated_at      TEXT NOT NULL DEFAULT ''
);
"""

# Columns selected back as a session dict. Kept as a constant so every read
# path returns the same shape.
_COLS = (
    "session_id, game, status, mode, backend, cdp_port, user_data_dir, "
    "container_name, vnc_port, egress, created_at, updated_at"
)

# v1 (skeleton: session_id/game/status/created_at/updated_at) → v2: bolt on the
# real-browser columns. Each ADD COLUMN is idempotent enough — the _migrate loop
# tolerates "duplicate column" if a prior partial run already added one.
_MIGRATIONS = {
    (1, 2): """\
ALTER TABLE rlm_sessions ADD COLUMN mode TEXT NOT NULL DEFAULT 'simple';
ALTER TABLE rlm_sessions ADD COLUMN backend TEXT NOT NULL DEFAULT 'host';
ALTER TABLE rlm_sessions ADD COLUMN cdp_port INTEGER NOT NULL DEFAULT 0;
ALTER TABLE rlm_sessions ADD COLUMN user_data_dir TEXT NOT NULL DEFAULT '';
ALTER TABLE rlm_sessions ADD COLUMN container_name TEXT NOT NULL DEFAULT '';
ALTER TABLE rlm_sessions ADD COLUMN vnc_port INTEGER NOT NULL DEFAULT 0;
ALTER TABLE rlm_sessions ADD COLUMN egress TEXT NOT NULL DEFAULT '';
""",
}

_initialized = False


def init() -> None:
    """Idempotently create the rlm-browser service's DB + ``rlm_sessions``."""
    global _initialized
    if not _initialized:
        init_service_db(
            SERVICE, SCHEMA_SQL, schema_version=SCHEMA_VERSION,
            migrations=_MIGRATIONS,
        )
        _initialized = True


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row(d: dict | None) -> dict | None:
    """Decode the stored ``egress`` JSON back into a dict for callers."""
    if d is None:
        return None
    out = dict(d)
    raw = out.get("egress") or ""
    try:
        out["egress"] = json.loads(raw) if raw else None
    except (TypeError, ValueError):
        out["egress"] = None
    return out


class BrowserDAO(BaseDAO):
    """CRUD over ``rlm_sessions`` (one row per acquired realm session)."""

    def __init__(self, conn: sqlite3.Connection | None = None) -> None:
        super().__init__(SERVICE, conn=conn)

    def create_session(
        self,
        game: str,
        *,
        mode: str = "headless",
        backend: str = "host",
        cdp_port: int = 0,
        user_data_dir: str = "",
        container_name: str = "",
        vnc_port: int = 0,
        egress: dict | None = None,
    ) -> dict:
        """Mint a new session row and return it. The browser is launched by the
        caller (the manager); this records how to reach + reconnect to it."""
        session_id = f"rlm-browser-{uuid.uuid4().hex[:12]}"
        now = _now()
        self.execute(
            """\
            INSERT INTO rlm_sessions (
                session_id, game, status, mode, backend, cdp_port,
                user_data_dir, container_name, vnc_port, egress,
                created_at, updated_at
            ) VALUES (?, ?, 'active', ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session_id, str(game or "").strip(), str(mode), str(backend),
                int(cdp_port), str(user_data_dir), str(container_name),
                int(vnc_port), json.dumps(egress) if egress else "",
                now, now,
            ),
        )
        return self.get_session(session_id)

    def get_session(self, session_id: str) -> dict | None:
        if not session_id:
            return None
        return _row(self.query_one(
            f"SELECT {_COLS} FROM rlm_sessions WHERE session_id = ?",
            (str(session_id).strip(),),
        ))

    def list_sessions(self) -> list[dict]:
        return [
            _row(r) for r in self.query_all(
                f"SELECT {_COLS} FROM rlm_sessions ORDER BY created_at"
            )
        ]

    def set_status(self, session_id: str, status: str) -> dict | None:
        """Update a session's status (e.g. 'active' → 'reset'); return the row."""
        sid = str(session_id).strip()
        self.execute(
            "UPDATE rlm_sessions SET status = ?, updated_at = ? WHERE session_id = ?",
            (str(status).strip(), _now(), sid),
        )
        return self.get_session(sid)

    def delete_session(self, session_id: str) -> bool:
        """Return True if a row was deleted."""
        rows = self.execute(
            "DELETE FROM rlm_sessions WHERE session_id = ?",
            (str(session_id).strip(),),
        )
        return rows > 0
