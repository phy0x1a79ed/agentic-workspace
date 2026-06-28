"""Agents service data access — agent_instances + agent_transcript tables.

Per the modular invariant, the agents service owns its own SQLite DB at
AWM_DIR/services/agents/agents.db. All SQL for agent_instances and
agent_transcript lives here; no direct DB calls in agent_instances.py.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone

from awm.persistence.dao import BaseDAO
from awm.persistence.databases import init_service_db

SERVICE = "agents"
SCHEMA_VERSION = 3

SCHEMA_SQL = """\
CREATE TABLE IF NOT EXISTS agent_instances (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    scope            TEXT NOT NULL,
    cli_session_id   TEXT,
    log_path         TEXT,
    started_at       INTEGER NOT NULL,
    ended_at         INTEGER,
    data             TEXT NOT NULL DEFAULT '{}',
    mode             TEXT NOT NULL DEFAULT 'conversational',
    task_ref         TEXT,
    agent_ref        TEXT,
    placement_token  TEXT
);
CREATE INDEX IF NOT EXISTS idx_agent_instances_scope_started
    ON agent_instances(scope, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_agent_instances_open
    ON agent_instances(scope) WHERE ended_at IS NULL;
-- A placement_token names exactly one live placement (the task-bound row that
-- currently holds it). NULLs are allowed, so the uniqueness is partial. On
-- respawn the old row's token is cleared before the new row reinserts it,
-- keeping the token stable while the constraint holds.
CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_instances_placement_token
    ON agent_instances(placement_token) WHERE placement_token IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_agent_instances_agent_ref
    ON agent_instances(agent_ref) WHERE agent_ref IS NOT NULL;

CREATE TABLE IF NOT EXISTS agent_transcript (
    id          TEXT PRIMARY KEY,
    scope       TEXT NOT NULL,
    kind        TEXT NOT NULL,
    body        TEXT NOT NULL DEFAULT '',
    meta        TEXT NOT NULL DEFAULT '{}',
    ts          INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_agent_transcript_scope_ts
    ON agent_transcript(scope, ts);
"""

# v1→v2: additive task-bounded placement columns + their indexes.
# v2→v3: DROP the ``project`` column from both tables — placements, transcript,
# and the live bus are keyed on ``scope`` (the globally-unique unit slug) ALONE.
# The scope-keyed indexes that referenced ``project`` are dropped first (SQLite
# forbids DROP COLUMN on an indexed column), then re-created on ``scope`` after
# the column is gone. SQLite 3.53 supports ``ALTER TABLE ... DROP COLUMN``.
MIGRATIONS: dict[tuple[int, int], str] = {
    (1, 2): (
        "ALTER TABLE agent_instances ADD COLUMN "
        "mode TEXT NOT NULL DEFAULT 'conversational';\n"
        "ALTER TABLE agent_instances ADD COLUMN task_ref TEXT;\n"
        "ALTER TABLE agent_instances ADD COLUMN agent_ref TEXT;\n"
        "ALTER TABLE agent_instances ADD COLUMN placement_token TEXT;\n"
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_instances_placement_token "
        "ON agent_instances(placement_token) WHERE placement_token IS NOT NULL;\n"
        "CREATE INDEX IF NOT EXISTS idx_agent_instances_agent_ref "
        "ON agent_instances(agent_ref) WHERE agent_ref IS NOT NULL;\n"
    ),
    (2, 3): (
        "DROP INDEX IF EXISTS idx_agent_instances_scope_started;\n"
        "DROP INDEX IF EXISTS idx_agent_instances_open;\n"
        "ALTER TABLE agent_instances DROP COLUMN project;\n"
        "CREATE INDEX IF NOT EXISTS idx_agent_instances_scope_started "
        "ON agent_instances(scope, started_at DESC);\n"
        "CREATE INDEX IF NOT EXISTS idx_agent_instances_open "
        "ON agent_instances(scope) WHERE ended_at IS NULL;\n"
        "DROP INDEX IF EXISTS idx_agent_transcript_scope_ts;\n"
        "ALTER TABLE agent_transcript DROP COLUMN project;\n"
        "CREATE INDEX IF NOT EXISTS idx_agent_transcript_scope_ts "
        "ON agent_transcript(scope, ts);\n"
    ),
}

_initialized = False


def init() -> None:
    global _initialized
    if not _initialized:
        init_service_db(SERVICE, SCHEMA_SQL, schema_version=SCHEMA_VERSION,
                        migrations=MIGRATIONS)
        _initialized = True


class AgentsDAO(BaseDAO):
    """CRUD over agent_instances and agent_transcript."""

    def __init__(self, conn: sqlite3.Connection | None = None) -> None:
        super().__init__(SERVICE, conn=conn)

    # -- agent_instances -------------------------------------------------------

    def open_instance(self, *, scope: str,
                      log_path: str | None,
                      cli_session_id: str | None,
                      started_at: int,
                      intent: str = "live") -> int:
        """Insert a new agent_instances row (ended_at=NULL). Returns the row id."""
        data = json.dumps({"intent": intent}, sort_keys=True)
        return self.execute(
            "INSERT INTO agent_instances "
            "(scope, cli_session_id, log_path, started_at, ended_at, data) "
            "VALUES (?, ?, ?, ?, NULL, ?)",
            (scope, cli_session_id, log_path, started_at, data),
        )

    def open_task_instance(self, *, scope: str,
                           log_path: str | None,
                           cli_session_id: str | None,
                           started_at: int,
                           mode: str,
                           task_ref: str | None,
                           agent_ref: str | None,
                           placement_token: str | None,
                           intent: str = "live") -> int:
        """Insert a task-bound agent_instances row (the placement record).

        Same row as a conversational instance, plus the task-binding columns.
        ``mode`` is the lifecycle discriminator (anything other than
        ``'conversational'`` is owned by the placement/supervision driver, not
        the resume driver). Returns the row id."""
        data = json.dumps({"intent": intent}, sort_keys=True)
        return self.execute(
            "INSERT INTO agent_instances "
            "(scope, cli_session_id, log_path, started_at, ended_at, "
            "data, mode, task_ref, agent_ref, placement_token) "
            "VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?, ?)",
            (scope, cli_session_id, log_path, started_at, data,
             mode, task_ref, agent_ref, placement_token),
        )

    def resolve_placement(self, token: str) -> dict | None:
        """Return the agent_instances row currently holding ``token``, or None.

        The token is unique among live placements (partial unique index), so
        this is a single-row lookup — the placement IS the row."""
        return self.query_one(
            "SELECT * FROM agent_instances WHERE placement_token=?", (token,))

    def get_open_placement_by_identity(self, scope: str) -> dict | None:
        """Return the live placement row for ``scope``, or None.

        A placement's ``scope`` IS its workspace-unit slug (globally unique), so
        a placed agent's identity (the slug) resolves to its own placement row
        WITHOUT the agent supplying a token. The live row is the one still
        holding a ``placement_token`` (cleared on respawn) with
        ``ended_at IS NULL`` — at most one per scope (the lifecycle reuses one
        unit and ``_await_scope_free`` guarantees the prior stage vacated before
        the next opens). ``placement_outcome`` (a logical close that precedes
        process teardown) is checked by the caller."""
        return self.query_one(
            "SELECT * FROM agent_instances "
            "WHERE scope=? AND placement_token IS NOT NULL "
            "AND ended_at IS NULL "
            "ORDER BY started_at DESC LIMIT 1",
            (scope,),
        )

    def clear_placement_token(self, instance_id: int) -> None:
        """Null a row's placement_token (used before a respawn reinserts it).

        Keeps the partial-unique invariant intact: the logical placement_token
        is stable across respawn, but only the live row ever holds it."""
        self.execute(
            "UPDATE agent_instances SET placement_token=NULL WHERE id=?",
            (instance_id,),
        )

    def merge_instance_data(self, instance_id: int, patch: dict) -> dict:
        """Read-modify-write merge ``patch`` into a row's ``data`` JSON.

        The single mutation primitive for placement progress (the placement
        spec, deliverable staging map, the done flag, the planner graph buffer,
        the attached flag). Returns the merged data dict. Shallow merge —
        callers pass whole sub-objects (e.g. the full ``staged`` map) when they
        need to replace rather than deep-merge."""
        row = self.query_one(
            "SELECT data FROM agent_instances WHERE id=?", (instance_id,))
        try:
            data = json.loads(row["data"] if row and row["data"] else "{}")
        except (TypeError, ValueError):
            data = {}
        data.update(patch)
        self.execute(
            "UPDATE agent_instances SET data=? WHERE id=?",
            (json.dumps(data, sort_keys=True), instance_id),
        )
        return data

    def close_placement(self, instance_id: int, *, outcome: str) -> None:
        """Mark a placement logically terminated (``data['placement_outcome']``).

        Distinct from :meth:`close_instance` (process ended): a placement closes
        when a terminal worker tool fires or supervision force-fails it, which
        may precede the subprocess teardown. Once set, the token no longer
        resolves to an *open* placement."""
        row = self.query_one(
            "SELECT data FROM agent_instances WHERE id=?", (instance_id,))
        try:
            data = json.loads(row["data"] if row and row["data"] else "{}")
        except (TypeError, ValueError):
            data = {}
        data["placement_outcome"] = outcome
        self.execute(
            "UPDATE agent_instances SET data=? WHERE id=?",
            (json.dumps(data, sort_keys=True), instance_id),
        )

    def close_instance(self, instance_id: int, *, ended_at: int,
                       exit_code: int | None,
                       intent_override: str | None = None) -> None:
        """Close an agent_instances row (set ended_at + merge into data JSON)."""
        row = self.query_one(
            "SELECT data FROM agent_instances WHERE id=?", (instance_id,))
        try:
            data = json.loads(row["data"] if row and row["data"] else "{}")
        except (TypeError, ValueError):
            data = {}
        if intent_override is not None:
            data["intent"] = intent_override
        data["exit_code"] = exit_code
        self.execute(
            "UPDATE agent_instances SET ended_at=?, data=? WHERE id=?",
            (ended_at, json.dumps(data, sort_keys=True), instance_id),
        )

    def set_instance_intent(self, instance_id: int, intent: str) -> None:
        """Persist why an instance is about to exit."""
        row = self.query_one(
            "SELECT data FROM agent_instances WHERE id=?", (instance_id,))
        try:
            data = json.loads(row["data"] if row and row["data"] else "{}")
        except (TypeError, ValueError):
            data = {}
        data["intent"] = intent
        self.execute(
            "UPDATE agent_instances SET data=? WHERE id=?",
            (json.dumps(data, sort_keys=True), instance_id),
        )

    def update_instance_cli_session_id(self, instance_id: int,
                                        cli_sid: str) -> None:
        self.execute(
            "UPDATE agent_instances SET cli_session_id=? WHERE id=?",
            (cli_sid, instance_id),
        )

    def get_instance(self, instance_id: int) -> dict | None:
        """Return the agent_instances row or None."""
        return self.query_one(
            "SELECT * FROM agent_instances WHERE id=?", (instance_id,))

    def get_latest_cli_session_id(self, scope: str) -> str | None:
        """Return the most recent cli_session_id for ``scope``, or None."""
        row = self.query_one(
            "SELECT cli_session_id FROM agent_instances "
            "WHERE scope=? AND cli_session_id IS NOT NULL "
            "ORDER BY started_at DESC LIMIT 1",
            (scope,),
        )
        return row["cli_session_id"] if row else None

    def list_instances(self, scope: str | None = None) -> list[dict]:
        """Return all agent_instances rows, with scope/agent_cli inline."""
        where = []
        params: list = []
        if scope:
            where.append("scope = ?")
            params.append(scope)
        sql = "SELECT * FROM agent_instances"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY id DESC"
        return self.query_all(sql, params)

    def close_all_open_instances(self, ended_at: int) -> list[dict]:
        """Close all agent_instances rows where ended_at IS NULL. Returns closed rows."""
        rows = self.query_all(
            "SELECT id, data FROM agent_instances WHERE ended_at IS NULL")
        for r in rows:
            try:
                data = json.loads(r["data"] or "{}")
            except (TypeError, ValueError):
                data = {}
            data["closed_by"] = "reconcile"
            data["reason"] = "daemon_restart"
            self.execute(
                "UPDATE agent_instances SET ended_at=?, data=? WHERE id=?",
                (ended_at, json.dumps(data, sort_keys=True), r["id"]),
            )
        return rows

    def get_latest_instance_for_scope(self, scope: str) -> dict | None:
        """Return the most recent agent_instances row for ``scope``."""
        return self.query_one(
            "SELECT * FROM agent_instances "
            "WHERE scope=? "
            "ORDER BY started_at DESC LIMIT 1",
            (scope,),
        )

    # -- agent_transcript ------------------------------------------------------

    def insert_transcript(self, *, scope: str,
                          kind: str, body: str,
                          meta: dict | None,
                          ts: int) -> str:
        """Insert one agent_transcript row. Returns the generated id."""
        row_id = uuid.uuid4().hex
        meta_str = json.dumps(meta or {})
        self.execute(
            "INSERT INTO agent_transcript (id, scope, kind, body, meta, ts) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (row_id, scope, kind, body, meta_str, ts),
        )
        return row_id

    def upsert_transcript(self, *, id: str, scope: str,
                          kind: str, body: str,
                          meta: dict | None,
                          ts: int) -> str:
        """Insert-or-update one agent_transcript row keyed by ``id``.

        The streamed assistant message is a single durable row whose stable id
        is the harness ``message_id``: the first finalized text block inserts
        it, later blocks (or a re-finalize) update body/meta/ts in place. One
        message → one row, so the backfill/live dedupe key and the UI coalesce
        key line up automatically. Returns the id (== the message id passed in).
        """
        meta_str = json.dumps(meta or {})
        self.execute(
            "INSERT INTO agent_transcript (id, scope, kind, body, meta, ts) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET "
            "body=excluded.body, meta=excluded.meta, ts=excluded.ts",
            (id, scope, kind, body, meta_str, ts),
        )
        return id

    def read_transcript(self, scope: str) -> list[dict]:
        """All transcript rows for ``scope``, ordered by ts."""
        return self.query_all(
            "SELECT * FROM agent_transcript "
            "WHERE scope=? ORDER BY ts, id",
            (scope,),
        )

    def read_transcript_after(self, scope: str, *,
                              after_ts: int | None = None,
                              after_id: str | None = None,
                              limit: int | None = None) -> list[dict]:
        """Transcript rows after a monotonic cursor (ts ms + id tiebreak).

        The cursor is the (ts, id) of the last act the consumer has already
        seen; rows are returned strictly after it, ordered by the same
        monotonic key the live bus publishes in. ``after_ts=None`` returns the
        whole transcript (the connect-time backfill from the start).
        """
        sql = "SELECT * FROM agent_transcript WHERE scope=?"
        params: list = [scope]
        if after_ts is not None:
            # Strictly-after on (ts, id): a later ts, or the same ts with a
            # greater id. id is a uuid hex (lexicographically comparable).
            sql += " AND (ts > ? OR (ts = ? AND id > ?))"
            params.extend([after_ts, after_ts, after_id or ""])
        sql += " ORDER BY ts, id"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(int(limit))
        return self.query_all(sql, params)

    def get_last_transcript_row(self, scope: str,
                                kinds: list[str]) -> dict | None:
        """Most recent transcript row for ``scope`` with kind in kinds."""
        placeholders = ",".join("?" * len(kinds))
        return self.query_one(
            f"SELECT * FROM agent_transcript "
            f"WHERE scope=? AND kind IN ({placeholders}) "
            f"ORDER BY ts DESC, id DESC LIMIT 1",
            [scope, *kinds],
        )
