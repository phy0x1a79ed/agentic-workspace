"""The task verbs on `operations` (sessions.md R57, R58), served from `awm.reporting.work`.

The names and shapes are the ones agreed with redux-queue, so its queue on alcor
replaces this backend without a caller noticing.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable

from awm import gatewayclient
from awm.reporting.task_ids import claude_uuid, session_key
from awm.reporting.work import ROLES, WORKER, Work

log = logging.getLogger("awm.reporting.task_verbs")

HIDDEN = ["cli", "http"]

_SESSION = {"name": "session_id", "type": "string",
            "description": "A session id in R9 form (`cc` + hex) or a Claude session UUID. Default: the calling session."}
_SHOWN = [{"name": "name", "type": "string", "description": "The session's name, for the board."},
          {"name": "rc_url", "type": "string", "description": "The session's Remote Control link, for the board."}]
_TASK = {"name": "task_id", "type": "string", "required": True, "description": "The 8-character task id."}

MANIFEST: list[dict[str, Any]] = [
    {"name": "task_open", "tool": "operations_task_open", "surfaces": HIDDEN, "effect": "write",
     "description": "Open a task with its main session and mint its task id. awm opens one for a "
                    "session that starts new work on its own; this verb is for services.",
     "params": [{"name": "title", "type": "string", "description": "The task's title."}, _SESSION, *_SHOWN]},
    {"name": "task_attach", "tool": "operations_task_attach", "surfaces": HIDDEN, "effect": "write",
     "description": "Put a session on a task, as `cx start` does for a task id the coordinator passed. "
                    "A task the session opened on its own folds into this one.",
     "params": [_TASK, {**_SESSION, "required": True},
                {"name": "role", "type": "string", "description": f"One of {', '.join(ROLES)}. Default: worker."},
                *_SHOWN]},
    {"name": "task_join", "tool": "operations_task_join", "effect": "write",
     "description": "Join your session to a running task by its task id, when Tony started you while "
                    "that task was running. Your own card folds into the task's card, and the task's "
                    "main session is told you joined.",
     "params": [_TASK, _SESSION]},
    {"name": "task_for_session", "tool": "operations_task_for_session", "effect": "read",
     "description": "The task id of a session, or null when it has none.",
     "params": [_SESSION]},
    {"name": "task_sessions", "tool": "operations_task_sessions", "effect": "read",
     "description": "Every session of a task, active or finished, with its role.",
     "params": [_TASK]},
    {"name": "task_get", "tool": "operations_task_get", "effect": "read",
     "description": "A task's record and its sessions.",
     "params": [_TASK]},
    {"name": "lineage", "tool": "operations_lineage", "effect": "read",
     "description": "A session's lineage (sessions.md R16). Hard edges: the sessions it came from, "
                    "nearest first, and the sessions it started or became after a clear. Soft edges: "
                    "the journals its own journals cite, with the session each covers.",
     "params": [_SESSION]},
    {"name": "session_parent", "tool": "operations_session_parent", "surfaces": HIDDEN, "effect": "write",
     "description": "Record who started a session, as `cx start` does for a calling session (R13).",
     "params": [{**_SESSION, "required": True},
                {"name": "parent", "type": "string", "required": True, "description": "The parent's session id."},
                {"name": "how", "type": "string", "description": "start or clear. Default: start."}]},
    {"name": "task_session_end", "tool": "operations_task_session_end", "surfaces": HIDDEN, "effect": "write",
     "description": "Mark a session of a task finished.",
     "params": [_TASK, {**_SESSION, "required": True}]},
]

Resolve = Callable[[int], Any]
Notify = Callable[[str, str], Awaitable[bool]]
Cites = Callable[[str], Awaitable[list[dict]]]


def _shown(args: dict) -> dict[str, str | None]:
    """The optional name and Remote Control link a caller passed, for the board."""
    return {k: (str(args[k]).strip()[:200] or None) if args.get(k) else None for k in ("name", "rc_url")}


async def journal_cites(session: str) -> list[dict]:
    """The citations of ``session``'s journals, from the journal service."""
    out = await gatewayclient.call("journal", "reads", {"session": session, "limit": 1}, timeout=60)
    return list(out.get("cites") or []) if isinstance(out, dict) else []


async def notify_main(job: str, text: str) -> bool:
    """Type ``text`` into the background session ``job``. False when it could not be sent."""
    try:
        out = await gatewayclient.call("reflection", "message", {"job": job, "text": text}, timeout=60)
    except Exception as exc:  # noqa: BLE001 — a failed notice never fails the join
        log.warning("reporting: could not tell %s about a join: %s", job, exc)
        return False
    return not (isinstance(out, dict) and out.get("ok") is False)


class TaskVerbs:
    def __init__(self, work: Work, node: str, resolve: Resolve, notify: Notify = notify_main,
                 cites: Cites = journal_cites) -> None:
        self.work, self.node, self._resolve, self._notify, self._cites = work, node, resolve, notify, cites

    async def _session(self, args: dict, required: bool = False) -> str:
        given = args.get("session_id")
        if given:
            return session_key(given)
        if required:
            raise ValueError("session_id is required")
        pid = args.get("_caller_pid")
        rec = await asyncio.to_thread(self._resolve, pid) if isinstance(pid, int) else None
        if not rec or not rec.get("sessionId"):
            raise ValueError("no calling session: pass session_id")
        return session_key(str(rec["sessionId"]))

    def _job_of(self, session: str) -> str | None:
        """The background job that holds ``session`` on this node, from the ledger."""
        raw = claude_uuid(session)
        if raw is None:
            return None
        row = self.work._one("SELECT key, kind FROM tasks WHERE node=? AND (session_id=? OR id IN"
                             " (SELECT task_id FROM task_aliases WHERE session_id=?)) ORDER BY id DESC",
                             (self.node, raw, raw))
        return row["key"] if row and row["kind"] == "bg" else None

    async def task_open(self, args: dict) -> dict:
        session = await self._session(args)
        task_id = await asyncio.to_thread(self.work.task_open, str(args.get("title") or ""), session, self.node,
                                          **_shown(args))
        return {"ok": True, "task_id": task_id}

    async def task_attach(self, args: dict) -> dict:
        session = await self._session(args, required=True)
        out = await asyncio.to_thread(self.work.task_attach, str(args.get("task_id") or ""), session,
                                      str(args.get("role") or WORKER), self.node, **_shown(args))
        return {"ok": True, **out}

    async def task_join(self, args: dict) -> dict:
        session = await self._session(args)
        out = await asyncio.to_thread(self.work.task_join, str(args.get("task_id") or ""), session, self.node)
        main = out.get("main_session_id")
        notified = False
        if main and main != session:
            job = await asyncio.to_thread(self._job_of, main)
            if job:
                notified = await self._notify(job, f"[awm] session {session} joined your task {out['task_id']}"
                                                   f" (operations task_sessions shows it).")
        return {"ok": True, **out, "main_notified": notified}

    async def task_for_session(self, args: dict) -> dict:
        session = await self._session(args)
        return {"ok": True, "session_id": session,
                "task_id": await asyncio.to_thread(self.work.task_for_session, session)}

    async def task_sessions(self, args: dict) -> dict:
        task_id = str(args.get("task_id") or "")
        return {"ok": True, "task_id": task_id,
                "sessions": await asyncio.to_thread(self.work.task_sessions, task_id)}

    async def task_get(self, args: dict) -> dict:
        return {"ok": True, "task": await asyncio.to_thread(self.work.task_get, str(args.get("task_id") or ""))}

    async def lineage(self, args: dict) -> dict:
        session = await self._session(args)
        up = await asyncio.to_thread(self.work.ancestors, session)
        down = await asyncio.to_thread(self.work.children, session)
        built_on: list[dict] | None
        try:
            built_on = await self._cites(session)
        except Exception as exc:  # noqa: BLE001 — the hard edges stand without the journal
            log.warning("reporting: lineage of %s has no citations: %s", session, exc)
            built_on = None
        return {"ok": True, "session_id": session,
                "task_id": await asyncio.to_thread(self.work.task_for_session, session),
                "started_by": up, "started": down, "built_on": built_on}

    async def session_parent(self, args: dict) -> dict:
        session = await self._session(args, required=True)
        made = await asyncio.to_thread(self.work.set_parent, session, str(args.get("parent") or ""),
                                       str(args.get("how") or "start"), self.node)
        return {"ok": True, "session_id": session, "recorded": made}

    async def task_session_end(self, args: dict) -> dict:
        session = await self._session(args, required=True)
        ended = await asyncio.to_thread(self.work.task_session_end, str(args.get("task_id") or ""), session)
        return {"ok": True, "ended": ended}

    async def call(self, verb: str, args: dict) -> dict:
        try:
            return await getattr(self, verb)(args)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}


VERBS = tuple(f["name"] for f in MANIFEST)


def task_for_session(session_id: str, db_path: Any = None) -> str | None:
    """The task id of a session, for reflection `whoami`, which imports this lazily."""
    from awm.reporting import config

    return Work(db_path or config.db_path()).task_for_session(session_id)
