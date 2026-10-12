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
from datetime import datetime
from typing import Any, Awaitable, Callable

from awm import gatewayclient
from awm.claudedaemon import roster
from awm.config import node_name

from awm.reporting import config
from awm.reporting.ledger import (ACTIVE, BLOCKED, CANCELLED, CLOSED, COLUMNS, DONE, OPEN, PLANNED,
                                  PROPOSED, REVIEW, Ledger)
from awm.reporting.record import TITLE_CHARS, cut_at_word, plan_title, render_ledger, render_record
from awm.reporting.swarmboard import BoardError, SwarmBoard

log = logging.getLogger("awm.reporting.tasks")

#: A Trilium note URL: the note is the last segment of the fragment.
NOTE_URL = re.compile(r"https?://[^\s#]+#(?:[\w-]+/)*([\w-]{4,})")
CLAIM_WORDS = re.compile(r"\b(claim|attach)\b", re.I)
JOURNAL_SERVICE = "journal"
JOURNAL_FETCH = "journal_fetch"
JOURNAL_TOPIC = "posts"


@dataclass(frozen=True)
class Who:
    key: str
    kind: str
    pid: int
    session_id: str
    cwd: str | None


def _posted_at(post: dict) -> float:
    """A journal post's time in epoch seconds. Posts carry ISO text; older ones carried ms."""
    ts = post.get("ts")
    if isinstance(ts, (int, float)):
        return ts / 1000.0
    try:
        return datetime.fromisoformat(str(ts)).timestamp()
    except ValueError:
        return 0.0


def blocked_marker(session_id: str):
    """The file the prompt hook checks to send the next prompt; the hook deletes it on unblock."""
    return roster.claude_home() / "awm-reporting" / f"{session_id}.blocked"


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
    result = await gatewayclient.call_maybe_peer(
        gatewayclient.peer_env("AWM_SOCIAL_PEER"), "social", "send",
        {"account": account, "channel": channel, "text": text})
    if isinstance(result, dict) and result.get("ok") is False:
        raise RuntimeError(f"social send refused: {result.get('error') or result}")


class Tasks:
    def __init__(self, ledger: Ledger, board: SwarmBoard, *, node: str | None = None,
                 resolve: Callable[[int], dict | None] = roster.session_by_pid,
                 job_state: Callable[[str], dict] = _job_state,
                 dm: Callable[[str], Awaitable[None]] = _send_dm,
                 alive: Callable[[int, str], bool] = lambda pid, start: roster.proc_start(pid) == start,
                 fetch: Callable[..., Awaitable[Any]] = gatewayclient.call) -> None:
        self.ledger = ledger
        self.board = board
        self.node = node or node_name()
        self._resolve = resolve
        self._job_state = job_state
        self._dm = dm
        self._alive = alive
        self._unseen: set[int] = set()
        self._fetch = fetch
        self._caught_up = 0.0
        self.subscription: gatewayclient.SupervisedSubscription | None = None
        self._missing: dict[int, int] = {}
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
        return self._pick_title(who.kind, who.key, plan_text)

    def _pick_title(self, kind: str, key: str, plan_text: str, fallback: str = "") -> str:
        """The plan's ``#`` heading, else the job's auto name, else its intent's first line, cut to a word."""
        heading = plan_title(plan_text)
        if heading:
            return cut_at_word(heading, TITLE_CHARS)
        state = self._job_state(key) if kind == "bg" else {}
        name = str(state.get("name") or "").strip()
        if name and state.get("nameSource") == "auto" and not name.startswith("claimed "):
            return cut_at_word(name, TITLE_CHARS)
        intent = next((ln.strip() for ln in str(state.get("intent") or "").splitlines() if ln.strip()), "")
        return cut_at_word(intent, TITLE_CHARS) if intent else fallback or f"session {key}"

    def _title_of(self, task: dict) -> str:
        """What an owned card should be called now. A rekeyed task keeps the title it had."""
        rekeyed = "/" in task["key"]
        return self._pick_title(task["kind"], task["key"], task["plan"],
                                fallback=task["title"] if rekeyed else "")

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
        self.ledger.update(task["id"], title=title, note_id=card, shown=COLUMNS[state], dirty=1,
                           owned=int(note_id is None))
        self.ledger.alias(task["id"], who.session_id)
        self.ledger.event(task["id"], "created", title)
        return self.ledger.get(task["id"])

    async def _task(self, who: Who, state: str) -> dict:
        return self._find(who) or await self._open(who, state)

    def _record(self, task: dict) -> str:
        live, removed = self.ledger.todos(task["id"])
        return render_record(task, live, removed, self.ledger.events(task["id"], "journal"))

    async def _write_card(self, task: dict, *, dry_run: bool = False) -> list[str]:
        """Put the record on the board: in an owned card's own body, else in the claimed card's Record child."""
        html = self._record(task)
        if not task["owned"]:
            if not dry_run:
                await self.board.write_record(task["note_id"], html)
            return ["rewrite Record child"]
        title = self._title_of(task)
        acts = ["rewrite body"] + ([f"retitle to {title!r}"] if title != task["title"] else [])
        if not dry_run:
            await self.board.write_card(task["note_id"], html, title)
            if title != task["title"]:
                self.ledger.update(task["id"], title=title)
        return acts

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
            await self._write_card(task)
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

    async def hook_session_clear(self, who: Who, args: dict) -> dict:
        """The conversation was cleared: file the open task under ``<job>/<old session id>``.

        The next plan in this job then opens a card of its own. The old task
        closes through the sweep, which no longer finds its key among the jobs.
        """
        task = self.ledger.find(self.node, who.key)
        if task is None or task["state"] not in OPEN:
            return {"ok": True, "tracked": False, "blocked": False}
        old = task["session_id"] or f"task{task['id']}"
        self.ledger.rekey(task["id"], f"{task['key']}/{old}")
        self.ledger.event(task["id"], "cleared", old)
        blocked_marker(old).unlink(missing_ok=True)
        return {"ok": True, "tracked": True, "task": task["id"], "blocked": False}

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
            self.ledger.update(task["id"], title=title, note_id=note_id, state=PLANNED, dirty=1, owned=0)
        self.ledger.event(task["id"], "claimed", note_id)
        await self.sync(task["id"])
        return self._result(task["id"])

    async def request_human(self, who: Who, args: dict) -> dict:
        kind = str(args.get("kind") or "").strip()
        if not kind:
            kind = "review" if args.get("summary") and not args.get("reason") else "blocked"
        if kind == "blocked":
            return await self._blocked(who, args)
        if kind == "review":
            return await self._review(who, args)
        raise ValueError("kind must be blocked or review")

    async def _blocked(self, who: Who, args: dict) -> dict:
        reason = str(args.get("reason") or "").strip()
        if not reason:
            raise ValueError("reason is required")
        urgent = args.get("urgent") is True
        task = await self._task(who, BLOCKED)
        already_urgent = task["state"] == BLOCKED and bool(task.get("urgent"))
        out = await self._set(task, BLOCKED, "blocked", reason, reason=reason, urgent=int(urgent),
                              blocked_at=time.time(), reminded=0)
        if who.session_id:
            marker = blocked_marker(who.session_id)
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.touch()
        if urgent and not already_urgent:
            out["notified"] = await self._notify(self.ledger.get(task["id"]), reminder=False)
        return out

    async def _review(self, who: Who, args: dict) -> dict:
        summary = str(args.get("summary") or args.get("reason") or "").strip()
        if not summary:
            raise ValueError("summary is required")
        task = await self._task(who, REVIEW)
        return await self._set(task, REVIEW, "review", summary, reason=summary, urgent=0)

    # -- conversion ----------------------------------------------------------

    async def convert(self, dry_run: bool = False) -> dict:
        """Bring this node's open cards to the current shape and stamp the Ops notices that lack a time."""
        async with self.lock:
            cards = []
            for task in self.ledger.open(self.node):
                if not task["note_id"]:
                    continue
                actions = await self._write_card(task, dry_run=dry_run)
                if task["owned"]:
                    dropped = await self.board.drop_records(task["note_id"], dry_run=dry_run)
                    actions += [f"delete Record child {note_id}" for note_id in dropped]
                cards.append({"task": task["id"], "card": task["note_id"], "actions": actions})
            ops = await self.board.stamp_ops(dry_run=dry_run)
        return {"ok": True, "dry_run": dry_run, "cards": cards, "ops": ops}

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

    # -- the watcher ---------------------------------------------------------

    async def sweep(self, rows: list[dict]) -> None:
        """One pass over this node's tasks: close the ended, follow Tony's board edits, retry the board.

        ``rows`` are the local ``reflection list_other`` rows. A background job with no row on
        two passes in a row was removed, and counts as stopped.
        """
        async with self.lock:
            jobs = {r.get("job"): r for r in rows}
            for task in self.ledger.open(self.node):
                await self._ending(task, jobs.get(task["key"]))
            await self._board()
            await self._catch_up()
            for task in self.ledger.open(self.node):
                if (task["state"] == BLOCKED and task["urgent"] and not task["reminded"]
                        and time.time() - (task["blocked_at"] or 0) >= config.remind_s()
                        and await self._notify(task, reminder=True)):
                    self.ledger.update(task["id"], reminded=1)

    async def _ending(self, task: dict, row: dict | None) -> None:
        if task["kind"] == "bg":
            if row is None:
                self._missing[task["id"]] = self._missing.get(task["id"], 0) + 1
                if self._missing[task["id"]] < 2:
                    return
            self._missing.pop(task["id"], None)
            state = row.get("state") if row else None
            if state == "gone":
                if not task["ended"]:
                    self.ledger.update(task["id"], ended=1, dirty=1)
                    self.ledger.event(task["id"], "ended", "the session died without a stop")
                return
            if state not in (None, "stopped"):
                if task["ended"]:
                    self.ledger.update(task["id"], ended=0, dirty=1)
                    self.ledger.event(task["id"], "resumed")
                return
        elif self._alive(task["pid"], task["key"].split(":", 1)[1]):
            return
        await self.close(task)

    async def _board(self) -> None:
        try:
            snap = await self.board.snapshot()
        except BoardError as exc:
            log.warning("reporting: cannot read the Swarm board: %s", exc)
            snap = None
        state_of = {name: state for state, name in COLUMNS.items()}
        for task in self.ledger.open(self.node):
            column = snap.get(task["note_id"]) if snap is not None else None
            if column is not None:
                self._unseen.discard(task["id"])
            if snap is not None and task["note_id"] and not task["dirty"] and column != task["shown"]:
                if column is None and task["id"] not in self._unseen:
                    self._unseen.add(task["id"])  # one sweep can read mid-drag; cancel on the second
                elif column is None:
                    self._unseen.discard(task["id"])
                    self.ledger.update(task["id"], dirty=1)
                    self.ledger.close(task["id"], CANCELLED)
                else:
                    self.ledger.event(task["id"], "dragged", f"{task['shown']} -> {column}")
                    self.ledger.update(task["id"], state=state_of[column], shown=column, dirty=1)
            if self.ledger.get(task["id"])["dirty"]:
                await self.sync(task["id"])
        for task in self.ledger.closed(self.node):
            if task["dirty"]:
                await self.sync(task["id"])

    # -- journals ------------------------------------------------------------

    async def subscribe(self) -> None:
        """Append each journal post to the Journal section of the open tasks in its scope. Never returns."""
        self.subscription = gatewayclient.SupervisedSubscription(
            f"reporting/{JOURNAL_SERVICE}.{JOURNAL_TOPIC}",
            lambda: gatewayclient.subscribe(JOURNAL_SERVICE, JOURNAL_TOPIC), self.on_post)
        await self.subscription.run()

    async def on_post(self, event: Any) -> None:
        post = event.get("post") if isinstance(event, dict) else None
        if isinstance(post, dict):
            async with self.lock:
                await self._journal(post)

    async def _journal(self, post: dict) -> None:
        body = str(post.get("body") or "").strip()
        if post.get("kind") != "journal" or not body:
            return
        meta = post.get("meta") if isinstance(post.get("meta"), dict) else {}
        title = str(meta.get("title") or "").strip()
        for task in self.ledger.open(self.node):
            if ((task["project"], task["scope"]) == (post.get("project"), post.get("scope"))
                    and _posted_at(post) >= task["started"]
                    and self.ledger.event(task["id"], "journal", f"{title}\n\n{body}" if title else body,
                                          ref=str(post["id"]))):
                self.ledger.update(task["id"], dirty=1)
                await self.sync(task["id"])

    async def _catch_up(self) -> None:
        """Read the recent journal of every open task's scope, for posts the subscription missed."""
        if time.time() - self._caught_up < config.catchup_interval_s():
            return
        self._caught_up = time.time()
        for project, scope in {(t["project"], t["scope"]) for t in self.ledger.open(self.node)
                               if t["project"] and t["scope"]}:
            try:
                reply = await self._fetch(JOURNAL_SERVICE, JOURNAL_FETCH, {
                    "project": project, "scope": scope, "kind": "journal", "limit": 50,
                    "order": "desc"}, timeout=60.0)
            except Exception as exc:  # noqa: BLE001 — the next catch-up carries it
                log.warning("reporting: journal catch-up for %s/%s failed: %s", project, scope, exc)
                continue
            for post in reversed((reply or {}).get("posts") or []):
                await self._journal(post)
