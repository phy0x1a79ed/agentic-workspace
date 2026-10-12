"""The task verbs: what a session's hooks and its own calls do to the ledger and the board.

Every verb names its session by the pid the gateway stamped on the call, never
by an argument. ``session_id`` is only a check against the record behind that
pid. The ledger changes first and carries a ``dirty`` mark; the board follows,
and a board failure leaves the mark for the next sweep to clear.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from awm import gatewayclient
from awm.claudedaemon import roster
from awm.config import node_name

from awm.reporting import config
from awm.reporting.ledger import (ACTIVE, BLOCKED, CANCELLED, CLOSED, COLUMNS, DONE, OPEN, PLANNED,
                                  PROPOSED, REVIEW, Ledger)
from awm.reporting.record import render_ledger, render_record
from awm.reporting.swarmboard import BoardError, SwarmBoard

log = logging.getLogger("awm.reporting.tasks")

#: A Trilium note URL: the note is the last segment of the fragment.
NOTE_URL = re.compile(r"https?://[^\s#]+#(?:[\w-]+/)*([\w-]{4,})")
CLAIM_WORDS = re.compile(r"\b(claim|attach)\b", re.I)
TITLE_CHARS = 120


@dataclass(frozen=True)
class Who:
    key: str
    kind: str
    pid: int
    session_id: str
    cwd: str | None


def _job_state(job: str) -> dict:
    try:
        data = json.loads((roster.default_jobs_dir() / job / "state.json").read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _flag(flags: list, name: str) -> str | None:
    for i, flag in enumerate(flags):
        if flag == name and i + 1 < len(flags):
            return str(flags[i + 1])
        if str(flag).startswith(name + "="):
            return str(flag).split("=", 1)[1]
    return None


async def _send_dm(text: str) -> None:
    account, channel = config.dm_target()
    await gatewayclient.call_maybe_peer(
        gatewayclient.peer_env("AWM_SOCIAL_PEER"), "social", "send",
        {"account": account, "channel": channel, "text": text})


class Tasks:
    def __init__(self, ledger: Ledger, board: SwarmBoard, *, node: str | None = None,
                 resolve: Callable[[int], dict | None] = roster.session_by_pid,
                 job_state: Callable[[str], dict] = _job_state,
                 dm: Callable[[str], Awaitable[None]] = _send_dm) -> None:
        self.ledger = ledger
        self.board = board
        self.node = node or node_name()
        self._resolve = resolve
        self._job_state = job_state
        self._dm = dm
        self.lock = asyncio.Lock()

    # -- entry ---------------------------------------------------------------

    async def call(self, verb: str, args: dict) -> dict:
        args = dict(args)
        pid = args.pop("_caller_pid", None)
        rec = self._resolve(pid) if isinstance(pid, int) else None
        if not rec:
            return {"ok": False, "error": "no calling session: this verb is for a running Claude Code session"}
        if args.get("session_id") and args["session_id"] != rec.get("sessionId"):
            return {"ok": False, "error": "session_id does not match the calling session"}
        bg = rec.get("kind") == "bg" and rec.get("jobId")
        who = Who(str(rec["jobId"]) if bg else f"{rec['pid']}:{rec.get('procStart')}",
                  "bg" if bg else "interactive", int(rec["pid"]), str(rec.get("sessionId") or ""),
                  rec.get("cwd"))
        try:
            async with self.lock:
                return await getattr(self, verb)(who, args)
        except (BoardError, ValueError) as exc:
            return {"ok": False, "error": str(exc)}

    def _result(self, task_id: int, **extra: Any) -> dict:
        task = self.ledger.get(task_id)
        return {"ok": True, "task": task_id, "state": task["state"], "card": task["note_id"],
                "blocked": task["state"] == BLOCKED, **extra}

    # -- tracking ------------------------------------------------------------

    def _intent(self, who: Who, plan_text: str = "") -> str:
        first = ""
        if who.kind == "bg":
            first = str(self._job_state(who.key).get("intent") or "")
        if not first.strip():
            first = next((m[2:] for m in plan_text.splitlines() if m.startswith("# ")), "")
        first = next((ln.strip() for ln in first.splitlines() if ln.strip()), "")
        return first[:TITLE_CHARS] or f"session {who.key}"

    def _touch(self, task: dict, who: Who) -> None:
        if who.session_id and who.session_id != task["session_id"]:
            if task["session_id"]:
                self.ledger.alias(task["id"], task["session_id"])
            self.ledger.alias(task["id"], who.session_id)
            self.ledger.update(task["id"], session_id=who.session_id)

    def _find(self, who: Who) -> dict | None:
        task = self.ledger.find(self.node, who.key)
        if task is not None and task["state"] in OPEN:
            self._touch(task, who)
            return task
        return None

    async def _open(self, who: Who, state: str, *, title: str = "", note_id: str | None = None) -> dict:
        """A task for this session in ``state``, on a card made now unless ``note_id`` names one."""
        title = title or self._intent(who)
        card = note_id or await self.board.create_card(title, COLUMNS[state])
        project, scope = config.project_scope(who.cwd)
        flags = self._job_state(who.key).get("respawnFlags") or [] if who.kind == "bg" else []
        task, _ = self.ledger.ensure(
            self.node, who.key, who.kind, state=state, pid=who.pid, session_id=who.session_id,
            project=project, scope=scope, model=_flag(flags, "--model"), effort=_flag(flags, "--effort"))
        self.ledger.update(task["id"], title=title, note_id=card, shown=COLUMNS[state], dirty=1)
        self.ledger.alias(task["id"], who.session_id)
        self.ledger.event(task["id"], "created", title)
        return self.ledger.get(task["id"])

    async def _task(self, who: Who, state: str) -> dict:
        return self._find(who) or await self._open(who, state)

    def _record(self, task: dict) -> str:
        live, removed = self.ledger.todos(task["id"])
        return render_record(task, live, removed, self.ledger.events(task["id"], "journal"))

    async def sync(self, task_id: int) -> bool:
        """Bring the board in line with the ledger for one task. False when the board refused."""
        task = self.ledger.get(task_id)
        if not task["note_id"]:
            return True
        try:
            if task["state"] == CANCELLED:
                await self.board.write_ledger(self.node, render_ledger(self.node, self.ledger.closed(self.node)))
                self.ledger.update(task_id, dirty=0)
                return True
            await self.board.write_record(task["note_id"], self._record(task))
            if task["state"] in CLOSED:
                node_note = await self.board.write_ledger(
                    self.node, render_ledger(self.node, self.ledger.closed(self.node)))
                await self.board.move(task["note_id"], into=node_note)
                self.ledger.update(task_id, dirty=0, shown=None)
            else:
                await self.board.move(task["note_id"], COLUMNS[task["state"]])
                self.ledger.update(task_id, dirty=0, shown=COLUMNS[task["state"]])
        except BoardError as exc:
            log.warning("reporting: task %s waits for the board: %s", task_id, exc)
            return False
        return True

    async def _set(self, task: dict, state: str, event: str, detail: str = "", **fields: Any) -> dict:
        self.ledger.update(task["id"], state=state, dirty=1, **fields)
        self.ledger.event(task["id"], event, detail)
        await self.sync(task["id"])
        return self._result(task["id"])

    async def close(self, task: dict, state: str = DONE, **fields: Any) -> None:
        self.ledger.update(task["id"], dirty=1, **fields)
        self.ledger.close(task["id"], state)
        await self.sync(task["id"])

    # -- hooks ---------------------------------------------------------------

    async def hook_plan(self, who: Who, args: dict) -> dict:
        text = str(args.get("plan_text") or "")
        task = self._find(who)
        if task is None:
            task = await self._open(who, PLANNED, title=self._intent(who, text))
        self.ledger.update(task["id"], plan=text, dirty=1)
        self.ledger.event(task["id"], "plan", str(args.get("plan_path") or ""))
        await self.sync(task["id"])
        return self._result(task["id"])

    async def hook_plan_approved(self, who: Who, args: dict) -> dict:
        text = str(args.get("plan_text") or "")
        task = self._find(who) or await self._open(who, ACTIVE, title=self._intent(who, text))
        return await self._set(task, ACTIVE, "approved", plan=text or task["plan"])

    async def hook_todos(self, who: Who, args: dict) -> dict:
        items = args.get("items")
        if not isinstance(items, list):
            raise ValueError("items must be a list")
        task = self._find(who)
        if task is None:
            return {"ok": True, "tracked": False, "blocked": False}
        if self.ledger.set_todos(task["id"], [i for i in items if isinstance(i, dict) and "id" in i]):
            self.ledger.update(task["id"], dirty=1)
            await self.sync(task["id"])
        return self._result(task["id"])

    async def hook_prompt(self, who: Who, args: dict) -> dict:
        prompt = str(args.get("prompt") or "")
        found = NOTE_URL.search(prompt)
        if found and CLAIM_WORDS.search(prompt):
            return await self._claim(who, found.group(1))
        task = self._find(who)
        if task is None:
            return {"ok": True, "tracked": False, "blocked": False}
        if task["state"] == BLOCKED:
            return await self._set(task, ACTIVE, "unblocked", reason="", urgent=0)
        return self._result(task["id"])

    # -- agent verbs ---------------------------------------------------------

    async def claim_task(self, who: Who, args: dict) -> dict:
        url = str(args.get("url") or "").strip()
        found = NOTE_URL.search(url)
        note_id = found.group(1) if found else (url if re.fullmatch(r"[\w-]{4,}", url) else "")
        if not note_id:
            raise ValueError("url must be the link to a Trilium note")
        return await self._claim(who, note_id)

    async def _claim(self, who: Who, note_id: str) -> dict:
        task = self._find(who)
        if task is not None and task["note_id"] == note_id:
            return self._result(task["id"])
        taken = next((t for t in self.ledger.open(self.node) if t["note_id"] == note_id), None)
        if taken is not None:
            raise ValueError(f"that task is already claimed here by {taken['key']}")
        await self.board.move(note_id, COLUMNS[PROPOSED])
        title = await self.board.title_of(note_id)
        await self.board.move(note_id, COLUMNS[PLANNED])
        if task is None:
            task = await self._open(who, PLANNED, title=title, note_id=note_id)
        else:
            self.ledger.update(task["id"], title=title, note_id=note_id, state=PLANNED, dirty=1)
        self.ledger.event(task["id"], "claimed", note_id)
        await self.sync(task["id"])
        return self._result(task["id"])

    async def request_human_user(self, who: Who, args: dict) -> dict:
        reason = str(args.get("reason") or "").strip()
        if not reason:
            raise ValueError("reason is required")
        urgent = args.get("urgent") is True
        task = await self._task(who, BLOCKED)
        out = await self._set(task, BLOCKED, "blocked", reason, reason=reason, urgent=int(urgent),
                              blocked_at=time.time(), reminded=0)
        if urgent:
            out["notified"] = await self._notify(self.ledger.get(task["id"]), reminder=False)
        return out

    async def request_human_review(self, who: Who, args: dict) -> dict:
        summary = str(args.get("summary") or "").strip()
        if not summary:
            raise ValueError("summary is required")
        task = await self._task(who, REVIEW)
        return await self._set(task, REVIEW, "review", summary, reason=summary, urgent=0)

    # -- telling Tony --------------------------------------------------------

    async def _notify(self, task: dict, *, reminder: bool) -> bool:
        base = config.trilium_url()
        link = f"\n{base}/#root/{task['note_id']}" if base and task["note_id"] else ""
        attach = f"claude attach {task['key']}" if task["kind"] == "bg" else "its terminal"
        text = (f"{'Still waiting: ' if reminder else ''}**{task['title'] or task['key']}** on "
                f"**{self.node}** needs you.\n{task['reason']}{link}\nAttach: `{attach}` on {self.node}")
        try:
            await self._dm(text)
        except Exception as exc:  # noqa: BLE001 — the block is recorded either way
            log.warning("reporting: could not DM Tony about task %s: %s", task["id"], exc)
            return False
        return True
