"""The task store: every task record, its sessions and its card state (fleet.md R106, queue.md R61 to R66).

Only the store writes a card's section and status (R66). Sessions move their card with
``task_report``. Tony moves a card only through an action the approver verified and
MACed with the shared action secret, so an agent cannot reach those verbs through the
gateway. Every change appends to ``events``. The Trilium writer and the relays read
changes from there by id, never from the board.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from awm.taskqueue.canon import canon
from awm.taskqueue.ids import is_task_id, mint_task_id

PROPOSED, NEEDS_YOU, WORKING, DONE = "proposed", "needs_you", "working", "done"
SECTIONS = (PROPOSED, NEEDS_YOU, WORKING, DONE)

# task_report state -> (section, status). Working carries no status tag (R61 item 3).
REPORT_STATES = {
    "working": (WORKING, None),
    "planned": (NEEDS_YOU, "planned"),
    "blocked": (NEEDS_YOU, "blocked"),
    "review": (NEEDS_YOU, "review"),
    "done": (DONE, "done"),
}
APPROVAL_OUTCOMES = ("approved", "denied", "expired", "cancelled")
ROLES = ("main", "worker", "joined")
ACTION_MAX_AGE_S = 120

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    task_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'task',
    section TEXT NOT NULL,
    status TEXT,
    peer TEXT,
    tags TEXT NOT NULL DEFAULT '[]',
    body_ref TEXT,
    meta TEXT NOT NULL DEFAULT '{}',
    parent_task TEXT,
    folded_into TEXT,
    created REAL NOT NULL,
    updated REAL NOT NULL,
    done_at REAL
);
CREATE INDEX IF NOT EXISTS tasks_section ON tasks(section, updated);
CREATE INDEX IF NOT EXISTS tasks_done ON tasks(done_at);
CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(task_id),
    role TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    node TEXT,
    name TEXT,
    rc_url TEXT,
    started REAL NOT NULL,
    ended REAL
);
CREATE INDEX IF NOT EXISTS sessions_task ON sessions(task_id);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    ts REAL NOT NULL,
    kind TEXT NOT NULL,
    data TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS applied_ops (
    op_id TEXT PRIMARY KEY,
    result TEXT NOT NULL,
    ts REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS used_actions (
    mac TEXT PRIMARY KEY,
    ts REAL NOT NULL
);
"""


class QueueError(Exception):
    """A refused call. The message names what was wrong."""


@dataclass
class Store:
    path: Path
    action_secret: bytes = b""
    clock: Any = time.time
    # note id -> body text. The writer sets it, so a spawn checks the body Tony saw.
    body_reader: Callable[[str], str] | None = None

    def __post_init__(self) -> None:
        self.path = Path(self.path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA foreign_keys=ON")
        self._db.executescript(SCHEMA)
        self._lock = threading.RLock()

    # -- plumbing ----------------------------------------------------------

    def _tx(self):
        store = self

        class _T:
            def __enter__(self):
                store._lock.acquire()
                store._db.execute("BEGIN IMMEDIATE")
                return store._db

            def __exit__(self, et, ev, tb):
                try:
                    store._db.execute("ROLLBACK" if et else "COMMIT")
                finally:
                    store._lock.release()

        return _T()

    def _event(self, db, task_id: str, kind: str, **data: Any) -> None:
        db.execute("INSERT INTO events(task_id, ts, kind, data) VALUES (?,?,?,?)",
                   (task_id, self.clock(), kind, json.dumps(data)))

    def _task(self, db, task_id: str) -> sqlite3.Row:
        row = db.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        if row is None:
            raise QueueError(f"no task {task_id!r}")
        if row["folded_into"]:
            return self._task(db, row["folded_into"])
        return row

    def _once(self, op_id: str | None, fn):
        """Run a relay write once. A replayed op_id returns the first result."""
        if not op_id:
            return fn()
        with self._lock:
            row = self._db.execute("SELECT result FROM applied_ops WHERE op_id=?",
                                   (op_id,)).fetchone()
            if row:
                return json.loads(row["result"])
            result = fn()
            self._db.execute("INSERT INTO applied_ops VALUES (?,?,?)",
                             (op_id, json.dumps(result), self.clock()))
            return result

    def _set_card(self, db, task_id: str, section: str, status: str | None, why: str,
                  **data: Any) -> None:
        now = self.clock()
        done_at = now if section == DONE else None
        db.execute("UPDATE tasks SET section=?, status=?, updated=?, done_at=? WHERE task_id=?",
                   (section, status, now, done_at, task_id))
        self._event(db, task_id, why, section=section, status=status, **data)

    # -- card view ---------------------------------------------------------

    def _card(self, db, row: sqlite3.Row, with_sessions: bool = True) -> dict[str, Any]:
        card = {
            "task_id": row["task_id"], "title": row["title"], "kind": row["kind"],
            "section": row["section"], "status": row["status"], "peer": row["peer"],
            "tags": json.loads(row["tags"]), "body_ref": row["body_ref"],
            "meta": json.loads(row["meta"]), "parent_task": row["parent_task"],
            "created": row["created"], "updated": row["updated"], "done_at": row["done_at"],
        }
        if with_sessions:
            card["sessions"] = self._sessions(db, row["task_id"])
        return card

    def _sessions(self, db, task_id: str) -> list[dict[str, Any]]:
        rows = db.execute("SELECT * FROM sessions WHERE task_id=? ORDER BY started",
                          (task_id,)).fetchall()
        return [{"session_id": r["session_id"], "role": r["role"], "active": bool(r["active"]),
                 "node": r["node"], "name": r["name"], "rc_url": r["rc_url"],
                 "started": r["started"], "ended": r["ended"]} for r in rows]

    def _add_session(self, db, task_id: str, session_id: str, role: str, node: str | None,
                     name: str | None, rc_url: str | None) -> None:
        if role not in ROLES:
            raise QueueError(f"role must be one of {ROLES}, not {role!r}")
        db.execute(
            "INSERT INTO sessions(session_id, task_id, role, node, name, rc_url, started) "
            "VALUES (?,?,?,?,?,?,?) ON CONFLICT(session_id) DO UPDATE SET task_id=excluded.task_id,"
            " role=excluded.role, active=1, ended=NULL,"
            " node=COALESCE(excluded.node, node), name=COALESCE(excluded.name, name),"
            " rc_url=COALESCE(excluded.rc_url, rc_url)",
            (session_id, task_id, role, node, name, rc_url, self.clock()))
        self._event(db, task_id, "session", session_id=session_id, role=role)

    # -- session verbs (the redux-sessions interface) ------------------------

    def task_open(self, title: str, session_id: str | None = None, *, task_id: str | None = None,
                  kind: str = "task", peer: str | None = None, node: str | None = None,
                  section: str = WORKING, status: str | None = None, tags: list[str] | None = None,
                  meta: dict | None = None, name: str | None = None, rc_url: str | None = None,
                  op_id: str | None = None) -> dict[str, Any]:
        """A new card (sessions.md R57 item 1). A relay may pass the id it minted while alcor was down."""
        if not title or not title.strip():
            raise QueueError("a task needs a title")
        if section not in SECTIONS:
            raise QueueError(f"section must be one of {SECTIONS}")

        def run():
            with self._tx() as db:
                tid = task_id or mint_task_id()
                if not is_task_id(tid):
                    raise QueueError(f"malformed task id {tid!r}")
                if db.execute("SELECT 1 FROM tasks WHERE task_id=?", (tid,)).fetchone():
                    raise QueueError(f"task id {tid!r} is taken")
                now = self.clock()
                db.execute(
                    "INSERT INTO tasks(task_id, title, kind, section, status, peer, tags, meta,"
                    " created, updated) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (tid, title.strip(), kind, section, status, peer or node,
                     json.dumps(tags or []), json.dumps(meta or {}), now, now))
                self._event(db, tid, "open", title=title, section=section, status=status)
                if session_id:
                    self._add_session(db, tid, session_id, "main", node, name, rc_url)
                return {"task_id": tid}
        return self._once(op_id, run)

    def task_attach(self, task_id: str, session_id: str, role: str = "worker", *,
                    node: str | None = None, name: str | None = None, rc_url: str | None = None,
                    op_id: str | None = None) -> dict[str, Any]:
        """Put a session on an existing card (R57 item 3)."""
        def run():
            with self._tx() as db:
                tid = self._task(db, task_id)["task_id"]
                self._add_session(db, tid, session_id, role, node, name, rc_url)
                return {"task_id": tid}
        return self._once(op_id, run)

    def task_join(self, task_id: str, session_id: str, *, node: str | None = None,
                  op_id: str | None = None) -> dict[str, Any]:
        """Join a running task and fold the joiner's own card into it (R57 item 5).

        The store notifies nobody: the singleton door carries no reflection (R104 item 4).
        The caller tells ``main_session_id``.
        """
        def run():
            with self._tx() as db:
                target = self._task(db, task_id)
                tid = target["task_id"]
                own = db.execute("SELECT task_id FROM sessions WHERE session_id=?",
                                 (session_id,)).fetchone()
                folded = None
                if own and own["task_id"] != tid:
                    folded = own["task_id"]
                    others = db.execute(
                        "SELECT COUNT(*) FROM sessions WHERE task_id=? AND session_id<>?",
                        (folded, session_id)).fetchone()[0]
                    if others == 0:
                        db.execute("UPDATE tasks SET folded_into=?, updated=? WHERE task_id=?",
                                   (tid, self.clock(), folded))
                        self._event(db, folded, "fold", into=tid)
                    else:
                        folded = None
                self._add_session(db, tid, session_id, "joined", node, None, None)
                main = db.execute(
                    "SELECT session_id FROM sessions WHERE task_id=? AND role='main' "
                    "ORDER BY started LIMIT 1", (tid,)).fetchone()
                return {"task_id": tid, "main_session_id": main["session_id"] if main else None,
                        "folded_task_id": folded}
        return self._once(op_id, run)

    def task_for_session(self, session_id: str) -> str | None:
        row = self._db.execute("SELECT task_id FROM sessions WHERE session_id=?",
                               (session_id,)).fetchone()
        if not row:
            return None
        with self._lock:
            return self._task(self._db, row["task_id"])["task_id"]

    def task_sessions(self, task_id: str) -> list[dict[str, Any]]:
        with self._lock:
            return self._sessions(self._db, self._task(self._db, task_id)["task_id"])

    def task_report(self, task_id: str, session_id: str | None = None, state: str = "",
                    reason: str | None = None, *, rc_url: str | None = None,
                    op_id: str | None = None) -> dict[str, Any]:
        """A session moves its own card: working, planned, blocked, review or done."""
        if state not in REPORT_STATES:
            raise QueueError(f"state must be one of {tuple(REPORT_STATES)}, not {state!r}")
        section, status = REPORT_STATES[state]

        def run():
            with self._tx() as db:
                row = self._task(db, task_id)
                if row["kind"] == "approval":
                    raise QueueError("an approval card moves only through the approver")
                tid = row["task_id"]
                if rc_url and session_id:
                    db.execute("UPDATE sessions SET rc_url=? WHERE session_id=?",
                               (rc_url, session_id))
                self._set_card(db, tid, section, status, "report", session_id=session_id,
                               reason=reason)
                return {"task_id": tid, "section": section, "status": status}
        return self._once(op_id, run)

    def task_session_end(self, task_id: str, session_id: str, *,
                         op_id: str | None = None) -> dict[str, Any]:
        def run():
            with self._tx() as db:
                tid = self._task(db, task_id)["task_id"]
                db.execute("UPDATE sessions SET active=0, ended=? WHERE session_id=? AND task_id=?",
                           (self.clock(), session_id, tid))
                self._event(db, tid, "session_end", session_id=session_id)
                return {"task_id": tid}
        return self._once(op_id, run)

    def task_get(self, task_id: str) -> dict[str, Any]:
        with self._lock:
            return self._card(self._db, self._task(self._db, task_id))

    def task_list(self, section: str | None = None, q: str | None = None,
                  before: float | None = None, limit: int = 50) -> dict[str, Any]:
        """Cards newest first. Done pages back on ``done_at < before`` (R61 item 4)."""
        limit = max(1, min(int(limit), 200))
        sql = "SELECT * FROM tasks WHERE folded_into IS NULL"
        args: list[Any] = []
        if section:
            if section not in SECTIONS:
                raise QueueError(f"section must be one of {SECTIONS}")
            sql += " AND section=?"
            args.append(section)
        if q:
            sql += " AND (title LIKE ? OR task_id=? OR tags LIKE ? OR peer=?)"
            args += [f"%{q}%", q, f"%{q}%", q]
        order = "updated"
        if section == DONE:
            order = "done_at"
            if before is not None:
                sql += " AND done_at < ?"
                args.append(before)
        sql += f" ORDER BY {order} DESC LIMIT ?"
        args.append(limit + 1)
        with self._lock:
            rows = self._db.execute(sql, args).fetchall()
            cards = [self._card(self._db, r, with_sessions=False) for r in rows[:limit]]
        nxt = cards[-1]["done_at"] if section == DONE and len(rows) > limit else None
        return {"cards": cards, "next_before": nxt}

    def events_since(self, after: int = 0, limit: int = 500) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM events WHERE id>? ORDER BY id LIMIT ?",
                                    (after, limit)).fetchall()
        return [{"id": r["id"], "task_id": r["task_id"], "ts": r["ts"], "kind": r["kind"],
                 "data": json.loads(r["data"])} for r in rows]

    def kv_get(self, key: str) -> str | None:
        with self._lock:
            row = self._db.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def kv_set(self, key: str, value: str) -> None:
        with self._lock:
            self._db.execute("INSERT INTO kv VALUES (?,?) ON CONFLICT(key) DO UPDATE SET "
                             "value=excluded.value", (key, value))

    # -- Tony's actions (R63), only through the approver --------------------

    def action_mac(self, action: str, task_id: str | None, args: dict, ts: float) -> str:
        body = canon({"action": action, "task_id": task_id, "args": args, "ts": ts})
        return hmac.new(self.action_secret, body, hashlib.sha256).hexdigest()

    def card_action(self, action: str, task_id: str | None, args: dict, ts: float,
                    mac: str) -> dict[str, Any]:
        """Apply one action the approver verified (queue.md R63, R66)."""
        if not self.action_secret:
            raise QueueError("no action secret is configured")
        want = self.action_mac(action, task_id, args, ts)
        if not hmac.compare_digest(want, mac):
            raise QueueError("the action MAC does not verify")
        if abs(self.clock() - ts) > ACTION_MAX_AGE_S:
            raise QueueError("the action is stale")
        with self._lock:
            try:
                self._db.execute("INSERT INTO used_actions VALUES (?,?)", (mac, self.clock()))
            except sqlite3.IntegrityError:
                raise QueueError("the action was already applied") from None
        handler = getattr(self, f"_act_{action}", None)
        if handler is None:
            raise QueueError(f"unknown action {action!r}")
        return handler(task_id, args)

    def _act_note_new(self, task_id, args) -> dict[str, Any]:
        """A Proposed card for Tony's Markdown note (R63 item 1). The writer makes the note."""
        return self.task_open(args.get("title") or "New note", section=PROPOSED, kind="note",
                              peer=args.get("peer"))

    def _act_spawn(self, task_id, args) -> dict[str, Any]:
        """Convert a Proposed card to a task on a peer (R63 item 2). The approver issues the order."""
        peer = args.get("peer")
        if not peer:
            raise QueueError("spawn needs a peer")
        with self._lock:
            row = self._task(self._db, task_id)
        body = self._checked_body(row, args.get("body_sha256"))
        with self._tx() as db:
            row = self._task(db, task_id)
            if row["section"] != PROPOSED:
                raise QueueError("only a Proposed card spawns")
            db.execute("UPDATE tasks SET kind='task', peer=? WHERE task_id=?", (peer, row["task_id"]))
            self._set_card(db, row["task_id"], WORKING, None, "spawn", peer=peer,
                           body_sha256=args.get("body_sha256"))
            return {"task_id": row["task_id"], "peer": peer, "title": row["title"],
                    "body_ref": row["body_ref"], "body": body}

    def _checked_body(self, row: sqlite3.Row, body_sha256: str | None) -> str | None:
        """The body of a Proposed note, read again from Trilium and matched to the hash Tony signed.

        None when no Trilium is configured. The caller reads outside the store lock, so a
        slow vault does not hold up other writes.
        """
        if self.body_reader is None or row["kind"] != "note":
            return None
        try:
            body = self.body_reader(row["body_ref"]) if row["body_ref"] else ""
        except Exception as exc:
            raise QueueError(f"the note body cannot be read: {exc}") from None
        if hashlib.sha256(body.encode()).hexdigest() != body_sha256:
            raise QueueError("the note changed after the panel showed it: select the card again")
        return body

    def _act_new_from(self, task_id, args) -> dict[str, Any]:
        """A new task from a Working or Done card (R63 item 3)."""
        with self._lock:
            src = self._task(self._db, task_id)
        if src["section"] not in (WORKING, DONE):
            raise QueueError("a new task starts only from a Working or Done card")
        peer = args.get("peer") or src["peer"]
        out = self.task_open(args.get("title") or f"{src['title']} (follow-up)", peer=peer,
                             meta={"from": src["task_id"]})
        with self._tx() as db:
            db.execute("UPDATE tasks SET parent_task=? WHERE task_id=?",
                       (src["task_id"], out["task_id"]))
        return {"task_id": out["task_id"], "peer": peer, "from": src["task_id"]}

    def _act_close(self, task_id, args) -> dict[str, Any]:
        with self._tx() as db:
            row = self._task(db, task_id)
            if row["kind"] == "approval":
                raise QueueError("an approval closes by approve, deny or cancel")
            self._set_card(db, row["task_id"], DONE, "done", "close")
            return {"task_id": row["task_id"], "section": DONE, "status": "done"}

    def _act_reject(self, task_id, args) -> dict[str, Any]:
        with self._tx() as db:
            row = self._task(db, task_id)
            if row["kind"] == "approval":
                raise QueueError("an approval closes by approve, deny or cancel")
            self._set_card(db, row["task_id"], DONE, "rejected", "reject")
            return {"task_id": row["task_id"], "section": DONE, "status": "rejected"}

    def _act_set_body_ref(self, task_id, args) -> dict[str, Any]:
        """The writer records the Trilium note that holds a Proposed card's body."""
        with self._tx() as db:
            db.execute("UPDATE tasks SET body_ref=? WHERE task_id=?", (args["body_ref"], task_id))
            return {"task_id": task_id}

    # -- approval cards (fleet.md R97 item 1) --------------------------------

    def approval_card(self, request_id: str, title: str, *, node: str | None,
                      meta: dict, mac: str, ts: float) -> dict[str, Any]:
        """Open the Needs-you card of one approval request. Only the approver calls this."""
        args = {"request_id": request_id, "title": title, "node": node, "meta": meta}
        want = self.action_mac("approval_card", None, args, ts)
        if not self.action_secret or not hmac.compare_digest(want, mac):
            raise QueueError("the approval MAC does not verify")
        return self.task_open(title, kind="approval", peer=node, section=NEEDS_YOU,
                              status="approval", meta={"request_id": request_id, **meta})

    def approval_resolve(self, task_id: str, outcome: str, *, mac: str,
                         ts: float) -> dict[str, Any]:
        if outcome not in APPROVAL_OUTCOMES:
            raise QueueError(f"outcome must be one of {APPROVAL_OUTCOMES}")
        want = self.action_mac("approval_resolve", task_id, {"outcome": outcome}, ts)
        if not self.action_secret or not hmac.compare_digest(want, mac):
            raise QueueError("the approval MAC does not verify")
        with self._tx() as db:
            row = self._task(db, task_id)
            self._set_card(db, row["task_id"], DONE, outcome, "approval", outcome=outcome)
            return {"task_id": row["task_id"], "status": outcome}
