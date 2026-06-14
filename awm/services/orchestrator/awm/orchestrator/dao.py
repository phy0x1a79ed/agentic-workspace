"""Orchestrator service data access — the durable plan, on the orchestrator
service's OWN SQLite DB (``AWM_DIR/services/orchestrator/orchestrator.db``).

Per the modular invariant there is no shared ``state.db``: this service owns
its tables and stands them up via :func:`init_service_db` at startup. The plan
is modelled in **four tables**, and the two graphs that make up the plan live
in **two different structures** so they can never be confused:

* **containment** is a tree — the ``tasks.parent_id`` self-FK (a task has ≤1
  parent, so containment cannot cycle and needs no edge table).
* **dependency** is its own ``edges`` table — "consumer task needs this
  contract"; the producer is read off the contract. Acyclicity is enforced
  **only** here, on dependency insert.

``contracts`` is the unit of hand-off between tasks (atomic name + spec +
delivery state); ``attempt_memories`` is the append-only record of every
attempt outcome — **never deleted**, so the plan stays reconstructable and a
re-planner can read why prior attempts failed.

No ``blocked`` / ``cancelled`` states and no competing-producer / coalescing /
OR tables — those are deferred to T5 (the data-locks lesson: don't add
machinery before it is used).
"""

from __future__ import annotations

import sqlite3
import time

from awm.persistence.dao import BaseDAO
from awm.persistence.databases import init_service_db, new_uuid

SERVICE = "orchestrator"
SCHEMA_VERSION = 1

# The 7-value node lifecycle. Work and review are symmetric:
#   a worker is needed:  ready  -> active   -> delivered   (terminal-ok)
#   a planner is needed: failed -> analyzing -> discarded   (terminal-give-up)
# ``pending`` covers both "leaf waiting on dependency contracts" and "composite
# parent waiting on its children to deliver".
STATES = (
    "pending", "ready", "active", "delivered", "failed", "analyzing", "discarded",
)

SCHEMA_SQL = """\
-- tasks — the plan nodes. Containment is the parent_id self-FK (a tree).
CREATE TABLE IF NOT EXISTS tasks (
    id              TEXT PRIMARY KEY,
    project         TEXT NOT NULL,
    goal            TEXT NOT NULL DEFAULT '',
    state           TEXT NOT NULL DEFAULT 'pending',
    parent_id       TEXT REFERENCES tasks(id),
    -- placement bookkeeping: which placement (if any) is currently out.
    mode            TEXT,            -- 'worker' | 'planner' | NULL (no placement out)
    scope_slug      TEXT,            -- flat slug minted at dispatch; cleared on reclaim
    agent_ref       TEXT,            -- the placed agent (from place_on_task) ; NULL when none
    placement_token TEXT,            -- opaque token returned by place_on_task
    replan_budget   INTEGER NOT NULL DEFAULT 2,   -- re-plan attempts left before discarded
    retry_count     INTEGER NOT NULL DEFAULT 0,   -- transient-error retries spent
    created_at      INTEGER NOT NULL,
    updated_at      INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tasks_project_state ON tasks(project, state);
CREATE INDEX IF NOT EXISTS idx_tasks_parent ON tasks(parent_id);

-- contracts — the unit of hand-off. Produced by exactly one task; delivered
-- once (payload_ref + delivered_ts set). ``name`` is the atomic contract name.
CREATE TABLE IF NOT EXISTS contracts (
    id            TEXT PRIMARY KEY,
    project       TEXT NOT NULL,
    name          TEXT NOT NULL,
    spec          TEXT NOT NULL DEFAULT '',
    producer_task TEXT NOT NULL REFERENCES tasks(id),
    payload_ref   TEXT,             -- artifact ref; NULL until delivered
    delivered_ts  INTEGER,          -- NULL until delivered
    created_at    INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_contracts_producer ON contracts(producer_task);
CREATE INDEX IF NOT EXISTS idx_contracts_project_name ON contracts(project, name);

-- edges — the dependency DAG, ONLY. "consumer_task needs contract_id"; the
-- producer is read off the contract. Acyclicity is checked on insert.
CREATE TABLE IF NOT EXISTS edges (
    id            TEXT PRIMARY KEY,
    consumer_task TEXT NOT NULL REFERENCES tasks(id),
    contract_id   TEXT NOT NULL REFERENCES contracts(id),
    created_at    INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_edges_consumer ON edges(consumer_task);
CREATE INDEX IF NOT EXISTS idx_edges_contract ON edges(contract_id);

-- attempt_memories — append-only outcome log. NEVER deleted.
CREATE TABLE IF NOT EXISTS attempt_memories (
    id          TEXT PRIMARY KEY,
    task_id     TEXT NOT NULL REFERENCES tasks(id),
    outcome     TEXT NOT NULL,      -- 'delivered' | 'failed'
    reason_type TEXT,               -- needs-decomposition | contract-unsatisfiable | transient-error | NULL
    reason_text TEXT NOT NULL DEFAULT '',
    payload_ref TEXT,               -- partial/delivered artifact ref, when any
    ts          INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_attempt_memories_task ON attempt_memories(task_id, ts);
"""

_initialized = False


def init() -> None:
    """Idempotently create the orchestrator service DB + all four tables."""
    global _initialized
    if not _initialized:
        init_service_db(SERVICE, SCHEMA_SQL, schema_version=SCHEMA_VERSION)
        _initialized = True


def _now() -> int:
    return int(time.time())


class OrchestratorDAO(BaseDAO):
    """CRUD over the orchestrator's own SQLite DB.

    Surfaces the full ``query_one`` / ``query_all`` / ``execute`` /
    ``transaction`` interface from :class:`BaseDAO`. Multi-table units of work
    (e.g. ``decompose_commit`` inserting children + contracts + edges) open a
    ``with self.transaction() as conn:`` block and pass ``conn`` to each helper
    so the whole mutation is atomic.
    """

    def __init__(self, conn: sqlite3.Connection | None = None) -> None:
        super().__init__(SERVICE, conn=conn)

    # -- tasks --------------------------------------------------------------

    def create_task(
        self,
        project: str,
        goal: str,
        *,
        state: str = "pending",
        parent_id: str | None = None,
        replan_budget: int = 2,
        conn: sqlite3.Connection | None = None,
    ) -> str:
        """Insert a task; return its new id."""
        tid = new_uuid()
        now = _now()
        self.execute(
            """INSERT INTO tasks
               (id, project, goal, state, parent_id, replan_budget,
                created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (tid, project, goal, state, parent_id, replan_budget, now, now),
            conn=conn,
        )
        return tid

    def get_task(
        self, task_id: str, *, conn: sqlite3.Connection | None = None
    ) -> dict | None:
        return self.query_one(
            "SELECT * FROM tasks WHERE id = ?", (task_id,), conn=conn
        )

    def list_tasks(
        self, project: str, *, conn: sqlite3.Connection | None = None
    ) -> list[dict]:
        return self.query_all(
            "SELECT * FROM tasks WHERE project = ? ORDER BY created_at",
            (project,), conn=conn,
        )

    def list_tasks_by_state(
        self, project: str, state: str, *,
        conn: sqlite3.Connection | None = None,
    ) -> list[dict]:
        return self.query_all(
            "SELECT * FROM tasks WHERE project = ? AND state = ? "
            "ORDER BY created_at",
            (project, state), conn=conn,
        )

    def list_children(
        self, parent_id: str, *, conn: sqlite3.Connection | None = None
    ) -> list[dict]:
        return self.query_all(
            "SELECT * FROM tasks WHERE parent_id = ? ORDER BY created_at",
            (parent_id,), conn=conn,
        )

    def update_task(
        self, task_id: str, *, conn: sqlite3.Connection | None = None, **fields
    ) -> None:
        """Patch named columns on a task; always bumps ``updated_at``.

        Allowed columns: state, mode, scope_slug, agent_ref, placement_token,
        replan_budget, retry_count, parent_id, goal.
        """
        allowed = {
            "state", "mode", "scope_slug", "agent_ref", "placement_token",
            "replan_budget", "retry_count", "parent_id", "goal",
        }
        cols = [c for c in fields if c in allowed]
        if not cols:
            return
        sets = ", ".join(f"{c} = ?" for c in cols) + ", updated_at = ?"
        params = [fields[c] for c in cols] + [_now(), task_id]
        self.execute(
            f"UPDATE tasks SET {sets} WHERE id = ?", params, conn=conn
        )

    # -- contracts ----------------------------------------------------------

    def create_contract(
        self,
        project: str,
        name: str,
        spec: str,
        producer_task: str,
        *,
        conn: sqlite3.Connection | None = None,
    ) -> str:
        cid = new_uuid()
        self.execute(
            """INSERT INTO contracts
               (id, project, name, spec, producer_task, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (cid, project, name, spec, producer_task, _now()),
            conn=conn,
        )
        return cid

    def get_contract(
        self, contract_id: str, *, conn: sqlite3.Connection | None = None
    ) -> dict | None:
        return self.query_one(
            "SELECT * FROM contracts WHERE id = ?", (contract_id,), conn=conn
        )

    def get_contract_by_name(
        self, project: str, name: str, *,
        conn: sqlite3.Connection | None = None,
    ) -> dict | None:
        return self.query_one(
            "SELECT * FROM contracts WHERE project = ? AND name = ? "
            "ORDER BY created_at LIMIT 1",
            (project, name), conn=conn,
        )

    def list_contracts_by_producer(
        self, producer_task: str, *, conn: sqlite3.Connection | None = None
    ) -> list[dict]:
        return self.query_all(
            "SELECT * FROM contracts WHERE producer_task = ?",
            (producer_task,), conn=conn,
        )

    def mark_contract_delivered(
        self, contract_id: str, payload_ref: str, *,
        conn: sqlite3.Connection | None = None,
    ) -> None:
        self.execute(
            "UPDATE contracts SET payload_ref = ?, delivered_ts = ? "
            "WHERE id = ?",
            (payload_ref, _now(), contract_id), conn=conn,
        )

    # -- edges (dependency DAG) ---------------------------------------------

    def create_edge(
        self, consumer_task: str, contract_id: str, *,
        conn: sqlite3.Connection | None = None,
    ) -> str:
        eid = new_uuid()
        self.execute(
            """INSERT INTO edges (id, consumer_task, contract_id, created_at)
               VALUES (?, ?, ?, ?)""",
            (eid, consumer_task, contract_id, _now()), conn=conn,
        )
        return eid

    def list_incoming_edges(
        self, consumer_task: str, *, conn: sqlite3.Connection | None = None
    ) -> list[dict]:
        """Edges where ``consumer_task`` is the consumer, joined to their
        contract's delivery state and producer."""
        return self.query_all(
            """SELECT e.id AS edge_id, e.contract_id,
                      c.name, c.producer_task, c.delivered_ts, c.payload_ref
               FROM edges e JOIN contracts c ON c.id = e.contract_id
               WHERE e.consumer_task = ?""",
            (consumer_task,), conn=conn,
        )

    def list_consumers_of_contract(
        self, contract_id: str, *, conn: sqlite3.Connection | None = None
    ) -> list[str]:
        """The task ids consuming a contract (to recompute their readiness when
        the contract delivers)."""
        rows = self.query_all(
            "SELECT consumer_task FROM edges WHERE contract_id = ?",
            (contract_id,), conn=conn,
        )
        return [r["consumer_task"] for r in rows]

    def depends_on(
        self, task_id: str, *, conn: sqlite3.Connection | None = None
    ) -> list[str]:
        """The producer task ids ``task_id`` depends on (one DFS hop): for each
        incoming edge, the producer of the consumed contract."""
        rows = self.query_all(
            """SELECT DISTINCT c.producer_task
               FROM edges e JOIN contracts c ON c.id = e.contract_id
               WHERE e.consumer_task = ?""",
            (task_id,), conn=conn,
        )
        return [r["producer_task"] for r in rows]

    # -- attempt memories (append-only) -------------------------------------

    def add_attempt_memory(
        self,
        task_id: str,
        outcome: str,
        *,
        reason_type: str | None = None,
        reason_text: str = "",
        payload_ref: str | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> str:
        mid = new_uuid()
        self.execute(
            """INSERT INTO attempt_memories
               (id, task_id, outcome, reason_type, reason_text, payload_ref, ts)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (mid, task_id, outcome, reason_type, reason_text, payload_ref, _now()),
            conn=conn,
        )
        return mid

    def list_attempt_memories(
        self, task_id: str, *, conn: sqlite3.Connection | None = None
    ) -> list[dict]:
        return self.query_all(
            "SELECT * FROM attempt_memories WHERE task_id = ? ORDER BY ts",
            (task_id,), conn=conn,
        )
