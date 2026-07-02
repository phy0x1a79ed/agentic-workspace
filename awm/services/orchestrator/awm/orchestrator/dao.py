"""Orchestrator service data access — the durable plan, on the orchestrator
service's OWN SQLite DB (``AWM_DIR/services/orchestrator/orchestrator.db``).

Per the modular invariant there is no shared ``state.db``: this service owns
its tables and stands them up via :func:`init_service_db` at startup. The plan
is **one** graph — a single global dependency DAG:

* **dependency** is the ``edges`` table — "consumer task needs this contract";
  the producer is read off the contract. Upstream = prerequisite, downstream =
  what comes next. Acyclicity is enforced here, on dependency insert. There is
  no containment tree: "the task that spawned a task" is exactly the consumer on
  a dependency edge, so a self-FK parent would carry no extra information.

A single special **root** task (``is_root = 1``) sits at the bottom of the DAG;
all user work is attached as a prerequisite (upstream) of root. Root is a
sentinel — it never gets a worker; it COMPLETES when every prerequisite is
delivered, and its abandonment escalates to the human.

``contracts`` is the unit of hand-off between tasks (atomic name + spec +
delivery state); ``attempt_memories`` is the append-only record of every
attempt outcome — **never deleted**, so the plan stays reconstructable and a
re-planner can read why prior attempts failed.

No competing-producer / coalescing / OR tables — those are deferred (the
data-locks lesson: don't add machinery before it is used).
"""

from __future__ import annotations

import sqlite3
import time

from awm.persistence.dao import BaseDAO
from awm.persistence.databases import init_service_db, new_uuid

SERVICE = "orchestrator"
SCHEMA_VERSION = 6

# The explicit node lifecycle — a TRUE state machine: every distinct position is
# its own named state. A *rest* state means a placement is needed (no agent
# live); the matching *out* state means one is live. ``agent_ref`` is pure data
# (which agent is placed) — it is set only on the four out-states and is never
# read to infer a lifecycle position.
#
#   rest                out            dispatch mode    meaning
#   ----                ---            -------------    -------
#   ready            -> planning       plan             deps met; plan the work
#   plan_delivered   -> verifying_plan verify           plan staged; check it
#   plan_approved    -> active         worker           plan ok; do the work
#   work_delivered   -> verifying_work accept           work claimed; verify it
#   decompose_pending-> decomposing    planner          expand into a sub-DAG
#
# ``blocked`` is a leaf waiting on its dependency contracts (not dispatchable
# until they deliver). A worker give-up rests in ``failed``; a planner give-up
# (budget out) rests in ``abandoned`` — both route their downstream consumers
# into ``decompose_pending`` to re-plan. ``completed`` is terminal-ok.
#
# ``work_delivered`` / ``verifying_work`` are the OPT-IN acceptance gate (mirrors
# the plan→verify leg one layer later): when a gated worker "delivers", its
# produced contracts are CLAIMED (payload_ref set, delivered_ts NULL — nothing
# completes) and the task rests ``work_delivered`` for an independent accept
# verifier (``accept`` mode) to run machine-checkable checks. Only ``accept_work``
# promotes the claim to a real delivery. A NULL ``accept_spec`` on every produced
# contract ⇒ the task is UNGATED ⇒ the legacy immediate-completion path.
STATES = (
    "blocked",
    "ready", "planning",
    "plan_delivered", "verifying_plan",
    "plan_approved", "active",
    "work_delivered", "verifying_work",
    "decompose_pending", "decomposing",
    "completed", "failed", "abandoned",
)

# The state machine as two flat tables — the single source of truth for
# dispatch (_prepare / _apply_placement) and kernel.reconcile. REST_MODE maps
# each *resting* state to the placement mode it needs; OUT_STATE maps that mode
# to the *out* state the node flips to once the placement is live. ``agent_ref``
# is never consulted to decide a position — these tables are.
REST_MODE = {
    "ready": "plan",
    "plan_delivered": "verify",
    "plan_approved": "worker",
    "work_delivered": "accept",
    "decompose_pending": "planner",
}
OUT_STATE = {
    "plan": "planning",
    "verify": "verifying_plan",
    "worker": "active",
    "accept": "verifying_work",
    "planner": "decomposing",
}

# The goal of the single global root sentinel (flagged by ``is_root = 1``; a
# task has no project, so the sentinel is identified by the flag alone).
ROOT_GOAL = "global root — all user work is a prerequisite of this node"

# The reserved pseudo-contract a ``plan`` agent delivers to hand its staged plan
# back to the kernel (``planning`` → ``plan_delivered``). It is NOT a row in the
# contracts table — its ref lands in ``tasks.plan_ref`` — and may not be used as
# a real produced-contract name.
PLAN_CONTRACT = "plan"

SCHEMA_SQL = """\
-- tasks — the plan nodes of the single global dependency DAG. A task has NO
-- project: its canonical key is its UUID ``id``. ``is_root`` flags the one root
-- sentinel (a worker is never placed on it). A task's attached git scopes live
-- in ``task_scopes`` (a task has 0+ scopes; a scope ≤1 non-terminal task).
CREATE TABLE IF NOT EXISTS tasks (
    id              TEXT PRIMARY KEY,
    goal            TEXT NOT NULL DEFAULT '',
    title           TEXT NOT NULL DEFAULT '',   -- human-set headline (separate from goal)
    tags            TEXT NOT NULL DEFAULT '[]',  -- JSON array of free-text tags (searchable)
    state           TEXT NOT NULL DEFAULT 'blocked',
    is_root         INTEGER NOT NULL DEFAULT 0,
    paused          INTEGER NOT NULL DEFAULT 0,   -- sticky human pause (freezes supervisor; survives detach)
    -- placement bookkeeping: which placement (if any) is currently out.
    mode            TEXT,            -- 'plan'|'verify'|'worker'|'planner' | NULL (no placement out)
    workspace_slug  TEXT,            -- workspace-service unit slug minted at dispatch; cleared on reclaim
    agent_ref       TEXT,            -- the placed agent (from place_on_task) ; NULL when none
    placement_token TEXT,            -- opaque token returned by place_on_task
    plan_ref        TEXT,            -- the staged plan artifact (delivered by a plan agent); NULL until planned
    attached        INTEGER NOT NULL DEFAULT 0,   -- DURABLE human-attached flag (single source of truth; freezes auto-progress)
    -- Steering handshake bits (the agents service is the single writer; the
    -- orchestrator only mirrors). Detach is a two-consent handshake: the agent
    -- sets ``steer_agent_ready`` (with the refreshed ``objective``), the user
    -- sets ``steer_user_done``; when both agree the agents side auto-detaches
    -- (clears attached + all three bits). ``steer_requested`` is a mid-run
    -- "wants steering" flag (set while detached; no freeze until a human attaches).
    steer_user_done   INTEGER NOT NULL DEFAULT 0,
    steer_agent_ready INTEGER NOT NULL DEFAULT 0,
    steer_requested   INTEGER NOT NULL DEFAULT 0,
    objective         TEXT NOT NULL DEFAULT '',   -- the durable objective record (system-written; ratified at detach)
    replan_budget   INTEGER NOT NULL DEFAULT 2,   -- re-attempts left (re-plans + decomposes) before abandoned
    accept_budget   INTEGER NOT NULL DEFAULT 2,   -- work-rejection re-attempts left (reject_work → worker) before contract-unsatisfiable
    retry_count     INTEGER NOT NULL DEFAULT 0,   -- transient-error retries spent
    created_at      INTEGER NOT NULL,
    updated_at      INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tasks_state ON tasks(state);
CREATE INDEX IF NOT EXISTS idx_tasks_root ON tasks(is_root) WHERE is_root = 1;

-- contracts — the unit of hand-off. Produced by exactly one task; delivered
-- once (payload_ref + delivered_ts set). ``name`` is the atomic contract name,
-- a single GLOBAL namespace (one DAG → one contract namespace).
CREATE TABLE IF NOT EXISTS contracts (
    id            TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    spec          TEXT NOT NULL DEFAULT '',
    producer_task TEXT NOT NULL REFERENCES tasks(id),
    accept_spec   TEXT,             -- JSON acceptance gate {objective, checks}; NULL=ungated (legacy path)
    payload_ref   TEXT,             -- artifact ref; NULL until delivered (a CLAIM when set + delivered_ts NULL)
    delivered_ts  INTEGER,          -- NULL until delivered
    created_at    INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_contracts_producer ON contracts(producer_task);
CREATE INDEX IF NOT EXISTS idx_contracts_name ON contracts(name);

-- task_scopes — a task's attached git scopes (it has 0+). Each row names one
-- scope by its scopes-service coordinate (project, scope) with the canonical
-- ``scope_ref`` = "project/scope"; ``name`` is the repos/<name> symlink the
-- unit links it under. Exclusivity (a scope_ref attached to ≤1 non-terminal
-- task) is a KERNEL invariant (kernel.check_scope_free), not a SQL constraint
-- (the holding task's state lives on ``tasks``).
CREATE TABLE IF NOT EXISTS task_scopes (
    id          TEXT PRIMARY KEY,
    task_id     TEXT NOT NULL REFERENCES tasks(id),
    name        TEXT NOT NULL,
    project     TEXT NOT NULL,
    scope       TEXT NOT NULL,
    scope_ref   TEXT NOT NULL,
    created_at  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_task_scopes_task ON task_scopes(task_id);
CREATE INDEX IF NOT EXISTS idx_task_scopes_ref ON task_scopes(scope_ref);
CREATE UNIQUE INDEX IF NOT EXISTS idx_task_scopes_task_name
    ON task_scopes(task_id, name);

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

# v1→v2: additive ``repos`` column on tasks (JSON list of existing scopes a
# node's workspace unit links under ``repos/<name>``). Idempotent.
# v2→v3 (P3 — decouple from the scopes project namespace): drop ``project`` from
# tasks + contracts (a task has no project; contracts are one global namespace),
# drop the per-task ``repos`` JSON (replaced by the first-class ``task_scopes``
# table), and swap the project-keyed indexes. SQLite 3.35+ supports ALTER TABLE
# DROP COLUMN, so the column drops + index swaps are direct.
# v3→v4 (this scope): add the human-facing task metadata — a separate ``title``,
# a JSON ``tags`` array (free-text, searchable), and a sticky ``paused`` flag
# (orthogonal to ``attached`` — it survives WS detach so the supervisor stays out
# while a human owns/left the task). All three carry constant defaults, so the
# ADD COLUMNs are direct.
# v4→v5 (steering protocol): the durable ``attached`` becomes the authoritative
# attach state and gains the steering-handshake columns — ``steer_user_done`` /
# ``steer_agent_ready`` / ``steer_requested`` (int 0/1 consent + wants-steering
# bits) and the ``objective`` record (text). All carry constant defaults, so the
# ADD COLUMNs are direct.
# v5→v6 (acceptance gate): add the opt-in independent-verification columns —
# ``contracts.accept_spec`` (JSON gate, NULL=ungated legacy path) and
# ``tasks.accept_budget`` (work-rejection re-attempts, constant default). Both are
# additive ADD COLUMNs (accept_spec NULL default, accept_budget constant), so old
# rows read as ungated with a fresh budget.
MIGRATIONS: dict[tuple[int, int], str] = {
    (1, 2): "ALTER TABLE tasks ADD COLUMN repos TEXT;\n",
    (2, 3): (
        "DROP INDEX IF EXISTS idx_tasks_project_state;\n"
        "DROP INDEX IF EXISTS idx_contracts_project_name;\n"
        "ALTER TABLE tasks DROP COLUMN project;\n"
        "ALTER TABLE tasks DROP COLUMN repos;\n"
        "ALTER TABLE contracts DROP COLUMN project;\n"
        "CREATE INDEX IF NOT EXISTS idx_tasks_state ON tasks(state);\n"
        "CREATE INDEX IF NOT EXISTS idx_contracts_name ON contracts(name);\n"
        "CREATE TABLE IF NOT EXISTS task_scopes (\n"
        "    id          TEXT PRIMARY KEY,\n"
        "    task_id     TEXT NOT NULL REFERENCES tasks(id),\n"
        "    name        TEXT NOT NULL,\n"
        "    project     TEXT NOT NULL,\n"
        "    scope       TEXT NOT NULL,\n"
        "    scope_ref   TEXT NOT NULL,\n"
        "    created_at  INTEGER NOT NULL\n"
        ");\n"
        "CREATE INDEX IF NOT EXISTS idx_task_scopes_task ON task_scopes(task_id);\n"
        "CREATE INDEX IF NOT EXISTS idx_task_scopes_ref ON task_scopes(scope_ref);\n"
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_task_scopes_task_name "
        "ON task_scopes(task_id, name);\n"
    ),
    (3, 4): (
        "ALTER TABLE tasks ADD COLUMN title TEXT NOT NULL DEFAULT '';\n"
        "ALTER TABLE tasks ADD COLUMN tags TEXT NOT NULL DEFAULT '[]';\n"
        "ALTER TABLE tasks ADD COLUMN paused INTEGER NOT NULL DEFAULT 0;\n"
    ),
    (4, 5): (
        "ALTER TABLE tasks ADD COLUMN steer_user_done INTEGER NOT NULL DEFAULT 0;\n"
        "ALTER TABLE tasks ADD COLUMN steer_agent_ready INTEGER NOT NULL DEFAULT 0;\n"
        "ALTER TABLE tasks ADD COLUMN steer_requested INTEGER NOT NULL DEFAULT 0;\n"
        "ALTER TABLE tasks ADD COLUMN objective TEXT NOT NULL DEFAULT '';\n"
    ),
    (5, 6): (
        "ALTER TABLE contracts ADD COLUMN accept_spec TEXT;\n"
        "ALTER TABLE tasks ADD COLUMN accept_budget INTEGER NOT NULL DEFAULT 2;\n"
    ),
}

_initialized = False


def init() -> None:
    """Idempotently create the orchestrator service DB + all four tables."""
    global _initialized
    if not _initialized:
        init_service_db(SERVICE, SCHEMA_SQL, schema_version=SCHEMA_VERSION,
                        migrations=MIGRATIONS)
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
        goal: str,
        *,
        state: str = "blocked",
        replan_budget: int = 2,
        title: str = "",
        conn: sqlite3.Connection | None = None,
    ) -> str:
        """Insert a task; return its new id (its canonical UUID key).

        ``title`` is the optional human-facing headline (a planner supplies one
        per decompose child; the born-attached / drop-in path leaves it empty and
        the steering agent sets it at detach via ``set_title``)."""
        tid = new_uuid()
        now = _now()
        self.execute(
            """INSERT INTO tasks
               (id, goal, title, state, replan_budget, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (tid, goal, title, state, replan_budget, now, now),
            conn=conn,
        )
        return tid

    def ensure_root(
        self, *, conn: sqlite3.Connection | None = None
    ) -> dict:
        """Return the single global root sentinel, creating it on first call.

        Root is born ``blocked`` (it depends on all user work) and never gets a
        worker; it is the escalation point and completes when its prerequisites
        do. ``replan_budget = 0`` — root never re-plans, it escalates."""
        row = self.get_root(conn=conn)
        if row is not None:
            return row
        tid = new_uuid()
        now = _now()
        self.execute(
            """INSERT INTO tasks
               (id, goal, state, is_root, replan_budget,
                created_at, updated_at)
               VALUES (?, ?, 'blocked', 1, 0, ?, ?)""",
            (tid, ROOT_GOAL, now, now), conn=conn,
        )
        return self.get_task(tid, conn=conn)

    def get_root(
        self, *, conn: sqlite3.Connection | None = None
    ) -> dict | None:
        return self.query_one(
            "SELECT * FROM tasks WHERE is_root = 1 LIMIT 1", conn=conn
        )

    def get_task(
        self, task_id: str, *, conn: sqlite3.Connection | None = None
    ) -> dict | None:
        return self.query_one(
            "SELECT * FROM tasks WHERE id = ?", (task_id,), conn=conn
        )

    def get_task_by_slug(
        self, slug: str, *, conn: sqlite3.Connection | None = None
    ) -> dict | None:
        """Resolve a task by its ``workspace_slug`` (the agent's unit-slug key).

        The privileged steering mirror keys by task id OR slug; a placed agent's
        identity is its slug, so this lets a mirror resolve without the task id."""
        return self.query_one(
            "SELECT * FROM tasks WHERE workspace_slug = ? "
            "ORDER BY created_at LIMIT 1",
            (slug,), conn=conn,
        )

    def list_tasks(
        self, *, conn: sqlite3.Connection | None = None
    ) -> list[dict]:
        """Every task in the single global DAG, oldest-first."""
        return self.query_all(
            "SELECT * FROM tasks ORDER BY created_at", conn=conn,
        )

    def list_tasks_by_state(
        self, state: str, *, conn: sqlite3.Connection | None = None,
    ) -> list[dict]:
        return self.query_all(
            "SELECT * FROM tasks WHERE state = ? ORDER BY created_at",
            (state,), conn=conn,
        )

    def update_task(
        self, task_id: str, *, conn: sqlite3.Connection | None = None, **fields
    ) -> None:
        """Patch named columns on a task; always bumps ``updated_at``.

        Allowed columns: state, mode, workspace_slug, agent_ref,
        placement_token, plan_ref, attached, replan_budget, accept_budget,
        retry_count, goal, title, tags, paused, steer_user_done,
        steer_agent_ready, steer_requested, objective.
        """
        allowed = {
            "state", "mode", "workspace_slug", "agent_ref", "placement_token",
            "plan_ref", "attached", "replan_budget", "accept_budget",
            "retry_count", "goal", "title", "tags", "paused", "steer_user_done",
            "steer_agent_ready", "steer_requested", "objective",
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
        name: str,
        spec: str,
        producer_task: str,
        *,
        accept_spec: str | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> str:
        """Insert a contract; return its new id.

        ``accept_spec`` is the opt-in acceptance gate (a JSON string) — NULL
        (the default) leaves the contract UNGATED, so its producer takes the
        legacy immediate-completion delivery path."""
        cid = new_uuid()
        self.execute(
            """INSERT INTO contracts
               (id, name, spec, producer_task, accept_spec, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (cid, name, spec, producer_task, accept_spec, _now()),
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
        self, name: str, *, conn: sqlite3.Connection | None = None,
    ) -> dict | None:
        """Resolve a contract by its name in the single global namespace."""
        return self.query_one(
            "SELECT * FROM contracts WHERE name = ? "
            "ORDER BY created_at LIMIT 1",
            (name,), conn=conn,
        )

    def list_contracts_by_producer(
        self, producer_task: str, *, conn: sqlite3.Connection | None = None
    ) -> list[dict]:
        return self.query_all(
            "SELECT * FROM contracts WHERE producer_task = ?",
            (producer_task,), conn=conn,
        )

    def list_all_contracts(
        self, *, conn: sqlite3.Connection | None = None,
    ) -> list[dict]:
        """Every contract in the global DAG — for the DAG snapshot read."""
        return self.query_all(
            "SELECT * FROM contracts ORDER BY created_at", conn=conn
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

    def set_contract_claim(
        self, contract_id: str, payload_ref: str, *,
        conn: sqlite3.Connection | None = None,
    ) -> None:
        """Record a gated worker's CLAIM on a contract: set ``payload_ref`` but
        leave ``delivered_ts`` NULL. A claim is not a delivery — nothing
        completes and no consumer advances until ``accept_work`` promotes it via
        :meth:`mark_contract_delivered`."""
        self.execute(
            "UPDATE contracts SET payload_ref = ? WHERE id = ?",
            (payload_ref, contract_id), conn=conn,
        )

    def clear_contract_claim(
        self, contract_id: str, *, conn: sqlite3.Connection | None = None,
    ) -> None:
        """Drop a claim (undelivered ``payload_ref``) on a rejected work
        attempt. Guarded ``WHERE delivered_ts IS NULL`` so it never un-delivers
        an already-accepted contract."""
        self.execute(
            "UPDATE contracts SET payload_ref = NULL "
            "WHERE id = ? AND delivered_ts IS NULL",
            (contract_id,), conn=conn,
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
                      c.name, c.spec, c.producer_task, c.delivered_ts,
                      c.payload_ref
               FROM edges e JOIN contracts c ON c.id = e.contract_id
               WHERE e.consumer_task = ?""",
            (consumer_task,), conn=conn,
        )

    def list_all_edges_joined(
        self, *, conn: sqlite3.Connection | None = None,
    ) -> list[dict]:
        """Every dependency edge in the global DAG, joined to its contract's
        name / producer / delivery state — denormalized so the DAG snapshot read
        carries both endpoints + the edge label in one row."""
        return self.query_all(
            "SELECT e.id AS edge_id, e.consumer_task, e.contract_id, "
            "       c.name AS contract_name, c.producer_task, "
            "       (c.delivered_ts IS NOT NULL) AS delivered "
            "FROM edges e JOIN contracts c ON c.id = e.contract_id "
            "ORDER BY e.created_at",
            conn=conn,
        )

    def delete_edges_for_contract(
        self, contract_id: str, *, conn: sqlite3.Connection | None = None
    ) -> int:
        """Drop every dependency edge that consumes a contract; returns the
        count removed. Used by ``relocate_task`` to re-point a node's funnel."""
        return self.execute(
            "DELETE FROM edges WHERE contract_id = ?", (contract_id,), conn=conn,
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

    # -- task_scopes (a task's attached git scopes) -------------------------

    def add_task_scope(
        self, task_id: str, name: str, project: str, scope: str,
        scope_ref: str, *, conn: sqlite3.Connection | None = None,
    ) -> str:
        """Attach a git scope to a task; return the attachment id.

        ``name`` is the repos/<name> link the unit mounts it under (unique per
        task); ``scope_ref`` = "project/scope" is the exclusivity key. Re-adding
        the same ``name`` replaces its coordinates (idempotent relink)."""
        rid = new_uuid()
        self.execute(
            """INSERT INTO task_scopes
               (id, task_id, name, project, scope, scope_ref, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(task_id, name) DO UPDATE SET
                   project = excluded.project,
                   scope = excluded.scope,
                   scope_ref = excluded.scope_ref""",
            (rid, task_id, name, project, scope, scope_ref, _now()),
            conn=conn,
        )
        return rid

    def list_task_scopes(
        self, task_id: str, *, conn: sqlite3.Connection | None = None
    ) -> list[dict]:
        """The scopes attached to a task, in the workspace ``repos`` shape
        (``[{name, project, scope, scope_ref}]``), oldest-first."""
        return self.query_all(
            "SELECT name, project, scope, scope_ref FROM task_scopes "
            "WHERE task_id = ? ORDER BY created_at",
            (task_id,), conn=conn,
        )

    def remove_task_scope(
        self, task_id: str, scope_ref: str, *,
        conn: sqlite3.Connection | None = None,
    ) -> int:
        """Detach a scope from a task by its ``scope_ref``; returns the count
        removed."""
        return self.execute(
            "DELETE FROM task_scopes WHERE task_id = ? AND scope_ref = ?",
            (task_id, scope_ref), conn=conn,
        )

    def tasks_holding_scope(
        self, scope_ref: str, *, conn: sqlite3.Connection | None = None
    ) -> list[dict]:
        """Every task that has ``scope_ref`` attached, with its current state —
        the kernel's exclusivity check reads this to find non-terminal holders."""
        return self.query_all(
            "SELECT DISTINCT ts.task_id AS id, t.state AS state "
            "FROM task_scopes ts JOIN tasks t ON t.id = ts.task_id "
            "WHERE ts.scope_ref = ?",
            (scope_ref,), conn=conn,
        )
