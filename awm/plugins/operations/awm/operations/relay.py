"""The node side of operations: forward to alcor, keep an outbox while it is away (fleet.md R106 item 6).

A write that cannot reach alcor goes into a local outbox under a sequence number and
the session gets its answer at once. Once anything waits in the outbox, later writes
queue behind it, so alcor sees one node's writes in the order the node made them. Each
write carries ``op_id = <node>:<seq>``, and the store applies an op_id once, so a replay
after a lost reply does nothing twice.

Approval requests and cancels ride the same outbox (R97 item 1). The relay mints a
request's id, as it mints a task id, so a request made while alcor is away has one.

The relay also pulls the spawn orders alcor signed for this node and starts each one
here. alcor never acts on another node (R97).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Protocol

from verify_grant import GrantInvalid, verify_order

from awm.operations.ids import mint_task_id

log = logging.getLogger("awm.operations")

WRITES = ("task_open", "task_attach", "task_join", "task_report", "task_session_end",
          "task_claim", "task_detail", "task_message", "approval_request", "approval_cancel",
          "order_done", "ops_post")
READS = ("task_for_session", "task_sessions", "task_get", "task_list", "task_messages",
         "approval_get", "keys")
# The writes that put a session on a card, so the relay can answer task_for_session offline.
SESSION_WRITES = ("task_open", "task_attach", "task_join", "task_claim")
# Long polls go straight to alcor: a stale copy would answer them wrongly.
POLLS = ("approval_wait", "order_pull")


class Unreachable(Exception):
    """alcor did not answer. The write is safe to replay."""


class Refused(Exception):
    """alcor answered with an error. Replaying the same write would fail the same way."""


class StoreClient(Protocol):
    async def call(self, verb: str, args: dict[str, Any]) -> Any: ...


SCHEMA = """
CREATE TABLE IF NOT EXISTS outbox (seq INTEGER PRIMARY KEY AUTOINCREMENT, verb TEXT NOT NULL,
    args TEXT NOT NULL, ts REAL NOT NULL);
CREATE TABLE IF NOT EXISTS dead (seq INTEGER PRIMARY KEY, verb TEXT, args TEXT, error TEXT, ts REAL);
CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, value TEXT NOT NULL, ts REAL NOT NULL);
CREATE TABLE IF NOT EXISTS seen_orders (order_id TEXT PRIMARY KEY, ts REAL NOT NULL);
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
"""


@dataclass
class Relay:
    node: str
    path: Path
    store: StoreClient
    clock: Callable[[], float] = time.time
    _db: sqlite3.Connection = field(init=False)
    _lock: threading.RLock = field(init=False, default_factory=threading.RLock)
    _flush_lock: asyncio.Lock = field(init=False, default_factory=asyncio.Lock)

    def __post_init__(self) -> None:
        self.path = Path(self.path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(SCHEMA)

    # -- state ---------------------------------------------------------------

    def _meta(self, k: str, v: str | None = None) -> str | None:
        with self._lock:
            if v is None:
                row = self._db.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
                return row["v"] if row else None
            self._db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (k, v))
            return v

    def _down(self) -> None:
        if self._meta("down_since") in (None, ""):
            self._meta("down_since", str(self.clock()))

    def _up(self) -> None:
        self._meta("down_since", "")

    def down_since(self) -> float | None:
        v = self._meta("down_since")
        return float(v) if v else None

    def pending(self) -> int:
        return self._db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]

    def _enqueue(self, verb: str, args: dict) -> int:
        with self._lock:
            cur = self._db.execute("INSERT INTO outbox(verb, args, ts) VALUES (?,?,?)",
                                   (verb, "{}", self.clock()))
            seq = cur.lastrowid
            args = {**args, "op_id": f"{self.node}:{seq}"}
            self._db.execute("UPDATE outbox SET args=? WHERE seq=?", (json.dumps(args), seq))
            return seq

    # -- writes ----------------------------------------------------------------

    async def write(self, verb: str, args: dict[str, Any]) -> Any:
        """Send a write, or queue it behind the outbox. Returns alcor's answer or a provisional one."""
        if verb not in WRITES:
            raise ValueError(f"{verb} is not a write")
        args = dict(args)
        if verb == "task_open":
            args.setdefault("task_id", mint_task_id())
            args.setdefault("node", self.node)
        if verb in ("task_attach", "task_join", "task_claim", "task_message"):
            args.setdefault("node", self.node)
        if verb == "approval_request":
            args.setdefault("request_id", "q" + secrets.token_urlsafe(9))
        seq = self._enqueue(verb, args)
        await self.flush()
        with self._lock:
            row = self._db.execute("SELECT 1 FROM outbox WHERE seq=?", (seq,)).fetchone()
            done = self._db.execute("SELECT value FROM cache WHERE key=?",
                                    (f"op:{seq}",)).fetchone()
            dead = self._db.execute("SELECT error FROM dead WHERE seq=?", (seq,)).fetchone()
        if dead:
            raise Refused(dead["error"])
        if done:
            return json.loads(done["value"])
        assert row is not None
        return self._provisional(verb, args)

    def _provisional(self, verb: str, args: dict) -> dict[str, Any]:
        out: dict[str, Any] = {"queued": True, "outbox": self.pending(),
                               "alcor_down_since": self.down_since()}
        for key in ("task_id", "request_id"):
            if key in args:
                out[key] = args[key]
        if verb == "task_join":
            out["main_session_id"] = None
        if verb in SESSION_WRITES and args.get("session_id"):
            self._remember_session(args["session_id"], args["task_id"])
        return out

    def _remember_session(self, session_id: str, task_id: str) -> None:
        with self._lock:
            self._db.execute("INSERT OR REPLACE INTO cache VALUES (?,?,?)",
                             (f"session:{session_id}", json.dumps(task_id), self.clock()))

    async def flush(self) -> int:
        """Send the outbox in order. Stops at the first unreachable write. Returns how many went."""
        sent = 0
        async with self._flush_lock:
            while True:
                with self._lock:
                    row = self._db.execute(
                        "SELECT * FROM outbox ORDER BY seq LIMIT 1").fetchone()
                if row is None:
                    return sent
                args = json.loads(row["args"])
                try:
                    result = await self.store.call(row["verb"], args)
                except Unreachable:
                    self._down()
                    return sent
                except Refused as exc:
                    log.warning("alcor refused %s %s: %s", row["verb"], args.get("op_id"), exc)
                    with self._lock:
                        self._db.execute("INSERT INTO dead VALUES (?,?,?,?,?)",
                                         (row["seq"], row["verb"], row["args"], str(exc),
                                          self.clock()))
                        self._db.execute("DELETE FROM outbox WHERE seq=?", (row["seq"],))
                    continue
                self._up()
                with self._lock:
                    self._db.execute("DELETE FROM outbox WHERE seq=?", (row["seq"],))
                    self._db.execute("INSERT OR REPLACE INTO cache VALUES (?,?,?)",
                                     (f"op:{row['seq']}", json.dumps(result), self.clock()))
                if isinstance(result, dict) and result.get("task_id") and args.get("session_id") \
                        and row["verb"] in SESSION_WRITES:
                    self._remember_session(args["session_id"], result["task_id"])
                sent += 1

    # -- reads -------------------------------------------------------------------

    async def read(self, verb: str, args: dict[str, Any]) -> Any:
        """Ask alcor. While it is away, answer from the last copy and say how old it is."""
        if verb not in READS:
            raise ValueError(f"{verb} is not a read")
        key = f"read:{verb}:{json.dumps(args, sort_keys=True)}"
        if self.pending() == 0:
            try:
                result = await self.store.call(verb, args)
            except Unreachable:
                self._down()
            else:
                self._up()
                with self._lock:
                    self._db.execute("INSERT OR REPLACE INTO cache VALUES (?,?,?)",
                                     (key, json.dumps(result), self.clock()))
                    if verb == "task_for_session" and result:
                        self._remember_session(args["session_id"], result)
                return result
        if verb == "task_for_session":
            row = self._db.execute("SELECT value FROM cache WHERE key=?",
                                   (f"session:{args['session_id']}",)).fetchone()
            return json.loads(row["value"]) if row else None
        row = self._db.execute("SELECT value, ts FROM cache WHERE key=?", (key,)).fetchone()
        return {"stale": True, "alcor_down_since": self.down_since(), "outbox": self.pending(),
                "as_of": row["ts"] if row else None,
                "result": json.loads(row["value"]) if row else None}

    async def poll(self, verb: str, args: dict[str, Any]) -> Any:
        """A long poll. Raises Unreachable while alcor is away or the outbox holds writes."""
        if verb not in POLLS:
            raise ValueError(f"{verb} is not a poll")
        if self.pending():
            raise Unreachable("the outbox still holds writes for alcor")
        try:
            out = await self.store.call(verb, args)
        except Unreachable:
            self._down()
            raise
        self._up()
        return out

    def mark(self, key: str, value: str | None = None, *, clear: bool = False) -> str | None:
        """A small local note, such as the card a session last sent to Needs you."""
        if clear:
            with self._lock:
                self._db.execute("DELETE FROM meta WHERE k=?", (key,))
            return None
        return self._meta(key, value)

    def marks(self, prefix: str) -> dict[str, str]:
        """Every mark whose key starts with ``prefix``."""
        with self._lock:
            rows = self._db.execute("SELECT k, v FROM meta WHERE substr(k, 1, ?) = ?",
                                    (len(prefix), prefix)).fetchall()
        return {r["k"]: r["v"] for r in rows}

    def status(self) -> dict[str, Any]:
        dead = self._db.execute("SELECT COUNT(*) FROM dead").fetchone()[0]
        return {"node": self.node, "outbox": self.pending(), "refused": dead,
                "alcor_down_since": self.down_since()}

    # -- spawn orders --------------------------------------------------------------

    def take_order(self, envelope: dict, pub) -> dict | None:
        """The order body if it is valid, for this node, and new. None for a replay."""
        body = verify_order(envelope, pub, node=self.node, now=self.clock())
        with self._lock:
            try:
                self._db.execute("INSERT INTO seen_orders VALUES (?,?)",
                                 (body["order_id"], self.clock()))
            except sqlite3.IntegrityError:
                return None
        return body


SLUG = re.compile(r"[^a-z0-9]+")


def session_name(title: str) -> str:
    """A legible session name from a card title (sessions.md R56): 2 to 3 words, under 40 chars."""
    words = [w for w in SLUG.sub(" ", title.lower()).split() if w][:3]
    return ("-".join(words) or "task")[:40]


async def run_order(body: dict, start: Callable[[dict], Awaitable[Any]],
                    defaults: dict[str, str]) -> Any:
    """Start the session one spawn or new_from order names, through reflection on this node."""
    p = body.get("payload", {})
    task_id = p["task_id"]
    text = p.get("body") or ""
    prompt = (f"Task {task_id}: {p.get('title', '')}\n\n{text}\n\n"
              f"Your task id is {task_id}. Report to its card with the operations tool.").strip()
    return await start({
        "project": p.get("project") or defaults["project"],
        "scope": p.get("scope") or defaults["scope"],
        "name": session_name(p.get("title", "")),
        "prompt": prompt,
        "task_id": task_id,
        "role": "main",
        "remote_control": True,
        "started_by": "Tony from the Swarm board",
    })


async def order_loop(relay: Relay, pull: Callable[[], Awaitable[list[dict]]], pub,
                     start: Callable[[dict], Awaitable[Any]],
                     done: Callable[[str, bool, str], Awaitable[Any]],
                     defaults: dict[str, str], *, stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            envelopes = await pull()
        except Unreachable:
            await asyncio.sleep(5)
            continue
        for env in envelopes:
            try:
                body = relay.take_order(env, pub)
            except GrantInvalid as exc:
                log.warning("dropped an order: %s", exc)
                continue
            if body is None:
                continue
            try:
                res = await run_order(body, start, defaults)
                await done(body["order_id"], True, json.dumps(res)[:500])
            except Exception as exc:  # report every failure to the card's order
                log.exception("order %s failed", body["order_id"])
                await done(body["order_id"], False, str(exc)[:500])
