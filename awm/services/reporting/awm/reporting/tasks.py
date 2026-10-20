"""The task verbs: what a session's hooks and its own calls do to the ledger and the board.

Every verb names its session by the pid the gateway stamped on the call, never
by an argument. ``session_id`` is only a check against the record behind that
pid. The ledger changes first and carries a ``dirty`` mark; the board follows,
and a board failure leaves the mark for the next sweep to clear.

A task can be linked to a federation card (``card_id``). Each transition of
such a task is mirrored onto the card with ``fed_state``, the last state the
mirror wrote. A card that sits somewhere else was moved by hand, and a manual
move wins: the mirror adopts it and skips that one transition.
"""

from __future__ import annotations

import asyncio
import hashlib
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
from awm.reporting.federation import AmbiguousRef, FedError, Federation
from awm.reporting.intake import PLAN_APPROVED, PLAN_SUBMITTED, card_state, send_dm, short_ref, swarm_of
from awm.reporting.ledger import (ACTIVE, BLOCKED, CANCELLED, CLOSED, COLUMNS, DONE,
                                  OLDER_SHOWN, OPEN, PLANNED, PROPOSED, REJECTED, REVIEW, Ledger)
from awm.reporting.record import TITLE_CHARS, cut_at_word, plan_title, render_ledger, render_record
from awm.reporting.sessions import GONE, STOPPED, CxUnavailable, Cx
from awm.reporting.store import Offloaded, Queue
from awm.reporting.swarmboard import BoardError, SwarmBoard

log = logging.getLogger("awm.reporting.tasks")

#: A Trilium note URL: the note is the last segment of the fragment.
NOTE_URL = re.compile(r"https?://[^\s#]+#(?:[\w-]+/)*([\w-]{4,})")
CLAIM_WORDS = re.compile(r"\b(claim|attach)\b", re.I)
#: "claim task <ref>", the line a card's direct message ends with.
CLAIM_TASK = re.compile(r"\bclaim\s+task\s+(\S+)", re.I)
_CARD_REF = re.compile(r"[0-9a-fA-F]{6,32}")
JOURNAL_SERVICE = "journal"
JOURNAL_FETCH = "fetch"
JOURNAL_TOPIC = "posts"
#: What a session hears when a card opens for its work, from a plan or `task_track`.
BOARD_NOTICE = (
    "Your work is now on Tony's Swarm board as the card \"{title}\", in {column}. "
    "Keep it current. Work that changes files belongs in its own scope: find or create it "
    "with scope(verb=\"search\") or scope(verb=\"create\"), then move there with "
    "reflection(verb=\"cd\", args={{path:<its worktree>}}). When you need Tony, call "
    "operations(verb=\"request_human\", args={{kind:\"blocked\", reason:<why>}}), which moves "
    "the card to Blocked. When the work is done, run the debrief skill. It ends with "
    "operations(verb=\"request_human\", args={{kind:\"review\", summary:<what to look at>}}), "
    "which moves the card to Pending review.")


@dataclass(frozen=True)
class Who:
    key: str
    kind: str
    pid: int | None
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


def _digest(text: str) -> str:
    return hashlib.sha1(text.encode()).hexdigest()[:10]


def _session_name(pid: Any) -> str | None:
    from awm.board import agent

    return agent.session_name(pid)


def blocked_marker(session_id: str):
    """The file the prompt hook checks to send the next prompt; the hook deletes it on unblock."""
    return roster.claude_home() / "awm-reporting" / f"{session_id}.blocked"


def _touch_marker(marker) -> None:
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.touch()


#: Ended-event details, rendered as the card's Ended line.
NOT_RUNNING = "not running, resumable"
STOPPED_DETAIL = "stopped"


def _job_exists(job: str) -> bool:
    return (roster.default_jobs_dir() / job).is_dir()


def _job_state(job: str) -> dict:
    try:
        data = json.loads((roster.default_jobs_dir() / job / "state.json").read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _scopes_of(project_dir) -> list[str]:
    try:
        return sorted(p.name for p in project_dir.iterdir()
                      if p.is_dir() and not p.name.startswith(".") and p.name != "data")[:50]
    except OSError:
        return []


def _claim_refusal(ref: str, exc: FedError) -> str:
    if exc.status == 409:
        return f"card {ref} is already held by another swarm or is finished: {exc}"
    if exc.status == 404:
        return f"card {ref} is not open to this swarm: {exc}"
    return f"the board would not let this swarm claim card {ref}: {exc}"


def _flag(flags: list, name: str) -> str | None:
    for i, flag in enumerate(flags):
        if flag == name and i + 1 < len(flags):
            return str(flags[i + 1])
        if str(flag).startswith(name + "="):
            return str(flag).split("=", 1)[1]
    return None


#: Federation card states the mirror writes for a task's ledger state.
FED_STATES = {PLANNED: "planned", ACTIVE: "active", BLOCKED: "blocked", REVIEW: "review",
              DONE: "done", REJECTED: "rejected"}


class Tasks:
    def __init__(self, ledger: Ledger, board: SwarmBoard, *, node: str | None = None,
                 resolve: Callable[[int], dict | None] = roster.session_by_pid,
                 job_state: Callable[[str], dict] = _job_state,
                 job_exists: Callable[[str], bool] = _job_exists,
                 dm: Callable[[str], Awaitable[None]] = send_dm,
                 alive: Callable[[int, str], bool] = lambda pid, start: roster.proc_start(pid) == start,
                 fetch: Callable[..., Awaitable[Any]] = gatewayclient.call,
                 queue: Queue | None = None, fed: Any = None, cx: Any = None,
                 agent_name: Callable[[Any], str | None] | None = None,
                 swarm: str | None = None) -> None:
        self.ledger = ledger
        self.db = Offloaded(ledger)
        self.queue = queue or Queue(ledger.path)
        self.qdb = Offloaded(self.queue)
        self.fed = fed or Federation()
        self.cx = cx or Cx()
        self._agent_name = agent_name or _session_name
        self.swarm = swarm or config.swarm()
        self._fed_checked: dict[int, float] = {}
        self.board = board
        self.node = node or node_name()
        self._resolve = resolve
        self._job_state = job_state
        self._job_exists = job_exists
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
        rec = await asyncio.to_thread(self._resolve, pid) if isinstance(pid, int) else None
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
        except (BoardError, FedError, ValueError) as exc:
            return {"ok": False, "error": str(exc)}

    async def _result(self, task_id: int, **extra: Any) -> dict:
        task = await self.db.get(task_id)
        return {"ok": True, "task": task_id, "state": task["state"], "card": task["note_id"],
                "blocked": task["state"] == BLOCKED, **extra}

    # -- tracking ------------------------------------------------------------

    async def _intent(self, who: Who, plan_text: str = "") -> str:
        return await self._pick_title(who.kind, who.key, plan_text)

    async def _pick_title(self, kind: str, key: str, plan_text: str, fallback: str = "") -> str:
        """The plan's ``#`` heading, else the job's name, else its intent's first line, cut to a word."""
        heading = plan_title(plan_text)
        if heading:
            return cut_at_word(heading, TITLE_CHARS)
        state = await asyncio.to_thread(self._job_state, key) if kind == "bg" else {}
        name = str(state.get("name") or "").strip()
        if name and state.get("nameSource") in ("auto", "user") and not name.startswith("claimed "):
            return cut_at_word(name, TITLE_CHARS)
        intent = next((ln.strip() for ln in str(state.get("intent") or "").splitlines() if ln.strip()), "")
        return cut_at_word(intent, TITLE_CHARS) if intent else fallback or f"session {key}"

    async def _title_of(self, task: dict) -> str:
        """What an owned card should be called now. Without a better name it keeps the title it had."""
        return await self._pick_title(task["kind"], task["key"], task["plan"], fallback=task["title"])

    async def _touch(self, task: dict, who: Who) -> None:
        if who.session_id and who.session_id != task["session_id"]:
            if task["session_id"]:
                await self.db.alias(task["id"], task["session_id"])
            await self.db.alias(task["id"], who.session_id)
            await self.db.update(task["id"], session_id=who.session_id)
        await self._follow(task, who.cwd)

    async def _follow(self, task: dict, cwd: str | None) -> None:
        """Put the task under the scope its session is in now, so a session that ran `/cd` follows."""
        if not cwd:
            return
        project, scope = await asyncio.to_thread(config.project_scope, cwd)
        if (project, scope) != (task["project"], task["scope"]):
            await self.db.update(task["id"], project=project, scope=scope, dirty=1)

    async def _find(self, who: Who) -> dict | None:
        task = await self.db.find(self.node, who.key)
        if task is not None and task["state"] in OPEN:
            await self._touch(task, who)
            return task
        return None

    async def _open(self, who: Who, state: str, *, title: str = "", note_id: str | None = None,
                    fresh: bool = False) -> dict:
        """A task for this session in ``state``, on a card made now unless ``note_id`` names one.

        ``fresh`` is new work: a closed task under the session's key is filed away
        instead of reopened. Without it a closed task is a resumed job, and reopens.
        """
        if fresh:
            old = await self.db.find(self.node, who.key)
            if old is not None and old["state"] in CLOSED:
                await self._file_away(old, "closed")
        title = title or await self._intent(who)
        card = note_id or await self.board.create_card(title, COLUMNS[state])
        project, scope = await asyncio.to_thread(config.project_scope, who.cwd)
        flags = (await asyncio.to_thread(self._job_state, who.key)).get("respawnFlags") or [] if who.kind == "bg" else []
        task, _ = await self.db.ensure(
            self.node, who.key, who.kind, state=state, pid=who.pid, session_id=who.session_id,
            project=project, scope=scope, model=_flag(flags, "--model"), effort=_flag(flags, "--effort"))
        await self.db.update(task["id"], title=title, note_id=card, shown=COLUMNS[state], dirty=1,
                             owned=int(note_id is None))
        if who.session_id:
            await self.db.alias(task["id"], who.session_id)
        await self.db.event(task["id"], "created", title)
        return await self.db.get(task["id"])

    async def _file_away(self, task: dict, why: str) -> None:
        """Move ``task`` off its session's key, so the session's next work opens a card of its own."""
        await self.db.rekey(task["id"], f"{task['key']}/{task['session_id'] or 'task'}-{task['id']}")
        await self.db.event(task["id"], "filed", why)

    async def _for_new_work(self, who: Who, state: str, text: str) -> tuple[dict, bool]:
        """The task a plan belongs to, and True when it was opened for it.

        A plan revises the open work, except that work waiting in review is
        finished: the plan is new work, on a new card.
        """
        task = await self._find(who)
        if task is not None and task["state"] == REVIEW:
            await self._file_away(task, "a new plan after review")
            task = None
        if task is not None:
            return task, False
        return await self._open(who, state, title=await self._intent(who, text), fresh=True), True

    async def _noticed(self, task_id: int, opened: bool, **extra: Any) -> dict:
        out = await self._result(task_id, opened=opened, **extra)
        if opened:
            task = await self.db.get(task_id)
            out["notice"] = BOARD_NOTICE.format(title=task["title"], column=COLUMNS[task["state"]])
        return out

    async def _task(self, who: Who, state: str) -> dict:
        return await self._find(who) or await self._open(who, state)

    async def _record(self, task: dict) -> str:
        live, removed = await self.db.todos(task["id"])
        ended = await self.db.events(task["id"], "ended")
        return render_record(task, live, removed, await self.db.events(task["id"], "journal"),
                             ended=ended[-1]["detail"] if ended else "")

    async def _write_card(self, task: dict, *, dry_run: bool = False) -> list[str]:
        """Put the record on the board: in an owned card's own body, else in the claimed card's Record child."""
        html = await self._record(task)
        if not task["owned"]:
            if not dry_run:
                await self.board.write_record(task["note_id"], html)
            return ["rewrite Record child"]
        title = await self._title_of(task)
        acts = ["rewrite body"] + ([f"retitle to {title!r}"] if title != task["title"] else [])
        if not dry_run:
            await self.board.write_card(task["note_id"], html, title)
            if title != task["title"]:
                await self.db.update(task["id"], title=title)
        return acts

    async def sync(self, task_id: int) -> bool:
        """Bring the boards in line with the ledger for one task. False when a board refused."""
        task = await self.db.get(task_id)
        if not task["note_id"]:
            return True
        try:
            if task["state"] == CANCELLED:
                await self.board.write_ledger(
                    self.node, render_ledger(self.node, await self.db.closed(self.node)))
                await self.db.update(task_id, dirty=0)
                return True
            await self._write_card(task)
            if task["state"] in CLOSED:
                await self.board.write_ledger(
                    self.node, render_ledger(self.node, await self.db.closed(self.node)))
            column = COLUMNS[task["state"]]
            if not (task["state"] in CLOSED and task["shown"] == OLDER_SHOWN):
                # A federation card carries the board's own cardStateAt, so it is only filled in.
                await self.board.move(task["note_id"], column, stamp="fill" if task["card_id"] else "move")
                await self.db.update(task_id, shown=column)
        except BoardError as exc:
            log.warning("reporting: task %s waits for the board: %s", task_id, exc)
            return False
        mirrored = await self._mirror(await self.db.get(task_id))
        await self.db.update(task_id, dirty=int(not mirrored))
        return mirrored

    async def _mirror(self, task: dict) -> bool:
        """Put the federation card in the state the task is in. False when the board should be tried again."""
        want = FED_STATES.get(task["state"])
        if not task["card_id"] or task["fed_state"] is None or want in (None, task["fed_state"]):
            return True
        try:
            actual = card_state(await self.fed.get(task["card_id"]))
            if actual == want:
                recorded = want
            elif actual != task["fed_state"]:
                log.info("reporting: card %s was moved to %s by hand; the mirror leaves it",
                         task["card_id"], actual)
                recorded = actual
            elif want == REJECTED:
                await self.fed.reject(task["card_id"], "rejected from the Swarm board", task["pid"])
                recorded = want
            else:
                await self.fed.status(task["card_id"], want)
                recorded = want
        except FedError as exc:
            if exc.retryable:
                log.warning("reporting: task %s waits for the federation board: %s", task["id"], exc)
                return False
            log.warning("reporting: the federation board refused task %s's move to %s: %s",
                        task["id"], want, exc)
            return True
        await self.db.update(task["id"], fed_state=recorded)
        return True

    async def _say_once(self, task: dict, who: Who, text: str, key: str) -> None:
        """Post ``text`` on the task's federation card thread, once per ``key``. A failure is logged."""
        if not task["card_id"] or task["fed_state"] is None:
            return
        if not await self.db.event(task["id"], "said", text, ref=f"{text}:{key}"):
            return
        try:
            await self.fed.message(task["card_id"], text, who.pid)
        except FedError as exc:
            log.warning("reporting: could not post %r on card %s: %s", text, task["card_id"], exc)

    async def _set(self, task: dict, state: str, event: str, detail: str = "", **fields: Any) -> dict:
        await self.db.update(task["id"], state=state, dirty=1, **fields)
        await self.db.event(task["id"], event, detail)
        await self.sync(task["id"])
        return await self._result(task["id"])

    async def close(self, task: dict, state: str = DONE, **fields: Any) -> None:
        await self.db.update(task["id"], dirty=1, **fields)
        await self.db.close(task["id"], state)
        await self.sync(task["id"])

    # -- hooks ---------------------------------------------------------------

    async def hook_plan(self, who: Who, args: dict) -> dict:
        text = str(args.get("plan_text") or "")
        task, opened = await self._for_new_work(who, PLANNED, text)
        await self.db.update(task["id"], plan=text, dirty=1)
        await self.db.event(task["id"], "plan", str(args.get("plan_path") or ""))
        await self.sync(task["id"])
        await self._say_once(task, who, PLAN_SUBMITTED, _digest(text))
        return await self._noticed(task["id"], opened)

    async def hook_plan_approved(self, who: Who, args: dict) -> dict:
        text = str(args.get("plan_text") or "")
        task, opened = await self._for_new_work(who, ACTIVE, text)
        await self._set(task, ACTIVE, "approved", plan=text or task["plan"])
        await self._say_once(task, who, PLAN_APPROVED, _digest(text or task["plan"]))
        return await self._noticed(task["id"], opened)

    async def hook_todos(self, who: Who, args: dict) -> dict:
        items = args.get("items")
        if not isinstance(items, list):
            raise ValueError("items must be a list")
        task = await self._find(who)
        if task is None:
            return {"ok": True, "tracked": False, "blocked": False}
        if await self.db.set_todos(task["id"], [i for i in items if isinstance(i, dict) and "id" in i]):
            await self.db.update(task["id"], dirty=1)
            await self.sync(task["id"])
        return await self._result(task["id"])

    async def hook_prompt(self, who: Who, args: dict) -> dict:
        prompt = str(args.get("prompt") or "")
        asked = CLAIM_TASK.search(prompt)
        if asked and (_CARD_REF.fullmatch(asked.group(1)) or NOTE_URL.search(asked.group(1))):
            return await self._claim_ref(who, asked.group(1))
        found = NOTE_URL.search(prompt)
        if found and CLAIM_WORDS.search(prompt):
            return await self._claim_ref(who, found.group(0))
        task = await self._find(who)
        if task is None:
            if args.get("first") and await self._briefed_new(who):
                task = await self._open(who, ACTIVE)
                await self.sync(task["id"])
                return await self._noticed(task["id"], True)
            return {"ok": True, "tracked": False, "blocked": False}
        if task["state"] == BLOCKED:
            return await self._set(task, ACTIVE, "unblocked", reason="", urgent=0)
        return await self._result(task["id"])

    async def _briefed_new(self, who: Who) -> bool:
        """Whether ``who`` is a worker cx started with the brief that the ledger has never held.

        Its first prompt opens its card, so a worker that finishes inside one sweep
        is still on the board. The sweep's ``_workers`` opens the card otherwise.
        """
        if who.kind != "bg" or await self.db.find(self.node, who.key):
            return False
        try:
            rows = await self.cx.list()
        except CxUnavailable:
            return False
        row = next((r for r in rows if r.get("job") == who.key), None)
        return bool(row and row.get("brief") and not row.get("pinned"))

    async def hook_session_clear(self, who: Who, args: dict) -> dict:
        """The conversation was cleared: file the open task under ``<job>/<old session id>``.

        The next plan in this job then opens a card of its own. The sweep, which
        no longer finds the old task's key among the jobs, moves it to Pending review.
        """
        task = await self.db.find(self.node, who.key)
        if task is None or task["state"] not in OPEN:
            return {"ok": True, "tracked": False, "blocked": False}
        old = task["session_id"] or f"task{task['id']}"
        await self.db.rekey(task["id"], f"{task['key']}/{old}")
        await self.db.event(task["id"], "cleared", old)
        await asyncio.to_thread(blocked_marker(old).unlink, missing_ok=True)
        return {"ok": True, "tracked": True, "task": task["id"], "blocked": False}

    # -- agent verbs ---------------------------------------------------------

    async def track_task(self, who: Who, args: dict) -> dict:
        """Put the calling session's work on the board: its open card, else a new one in Active."""
        task = await self._find(who)
        if task is not None:
            return await self._noticed(task["id"], False, title=task["title"],
                                       column=COLUMNS[task["state"]])
        title = cut_at_word(str(args.get("title") or "").strip(), TITLE_CHARS)
        task = await self._open(who, ACTIVE, title=title, fresh=True)
        await self.sync(task["id"])
        task = await self.db.get(task["id"])
        return await self._noticed(task["id"], True, title=task["title"],
                                   column=COLUMNS[task["state"]])

    async def claim_task(self, who: Who, args: dict) -> dict:
        text = str(args.get("ref") or args.get("url") or "").strip()
        text = re.sub(r"^claim\s+task\s+", "", text, flags=re.I)
        if not text:
            raise ValueError("ref is required: a federation card reference or a Trilium note URL")
        return await self._claim_ref(who, text.split()[0])

    async def _claim_ref(self, who: Who, ref: str) -> dict:
        """Claim what ``ref`` names: a federation card, else a Trilium note."""
        try:
            card = await self.fed.resolve(ref)
        except AmbiguousRef:
            raise
        except FedError as exc:
            if _CARD_REF.fullmatch(ref):
                raise ValueError(f"cannot look up card {ref}: {exc}") from exc
            card = None
        if card is not None:
            return await self._claim_card(who, card)
        found = NOTE_URL.search(ref)
        if _CARD_REF.fullmatch(ref) and not found:
            raise ValueError(f"no federation card matches {ref}")
        note_id = found.group(1) if found else (ref if re.fullmatch(r"[\w-]{4,}", ref) else "")
        if not note_id:
            raise ValueError("ref must be a federation card reference or the link to a Trilium note")
        return await self._claim(who, note_id)

    async def _claim(self, who: Who, note_id: str) -> dict:
        task = await self._find(who)
        if task is not None and task["note_id"] == note_id:
            return await self._result(task["id"])
        taken = next((t for t in await self.db.open(self.node) if t["note_id"] == note_id), None)
        if taken is not None:
            raise ValueError(f"that task is already claimed here by {taken['key']}")
        await self.board.move(note_id, COLUMNS[PROPOSED])
        title = await self.board.title_of(note_id)
        await self.board.move(note_id, COLUMNS[PLANNED])
        if task is None:
            task = await self._open(who, PLANNED, title=title, note_id=note_id)
        else:
            await self.db.update(task["id"], title=title, note_id=note_id, state=PLANNED, dirty=1, owned=0)
        await self.db.event(task["id"], "claimed", note_id)
        await self.sync(task["id"])
        return await self._result(task["id"])

    async def _claim_card(self, who: Who, card: dict) -> dict:
        """Claim a federation card for the calling session and show it on the Swarm board in Planned."""
        card_id, ref = card["id"], short_ref(card["id"])
        held = await self.db.open_for_card(card_id)
        if held is not None and (held["node"], held["key"]) == (self.node, who.key):
            return await self._claimed(who, held, card)
        if held is not None:
            if card_state(card) != "proposed":
                raise ValueError(f"card {ref} is already held here by {held['key']}")
            await self.close(held, CANCELLED, fed_state=None)  # Tony released the claim
        mine = await self._find(who)
        if mine is not None and mine["card_id"] and mine["card_id"] != card_id:
            raise ValueError(f"this session already works on card {short_ref(mine['card_id'])}; "
                             "finish or reject it first")
        self._refuse_if_held_elsewhere(card, ref)
        fed_state = None
        if card.get("kind") == "request":
            try:
                card = await self.fed.claim(card_id, who.pid)
            except FedError as exc:
                raise ValueError(_claim_refusal(ref, exc)) from exc
            fed_state = card_state(card)
        note_id = card.get("note_id")
        if not note_id:
            raise ValueError("the board did not report the card's Trilium note; upgrade the board host")
        await self.board.move(note_id, COLUMNS[PLANNED])
        await self.board.carry_labels(note_id, {
            "cardFrom": swarm_of(card.get("sender")), "cardTo": card.get("recipient"),
            "project": card.get("project"), "owner": card.get("owner")})
        title = card.get("title") or ref
        if mine is None:
            mine = await self._open(who, PLANNED, title=title, note_id=note_id)
        else:
            await self.db.update(mine["id"], title=title, note_id=note_id, state=PLANNED, dirty=1, owned=0)
        await self.db.update(mine["id"], card_id=card_id, fed_state=fed_state)
        name = self._agent_name(who.pid) or who.key
        await self.qdb.take(card, name, who.key if who.kind == "bg" else None)
        await self.db.event(mine["id"], "claimed", card_id)
        await self.sync(mine["id"])
        return await self._claimed(who, await self.db.get(mine["id"]), card)

    def _refuse_if_held_elsewhere(self, card: dict, ref: str) -> None:
        """Refuse a card another session of this swarm holds on another node."""
        owner = str(card.get("owner") or "")
        parts = owner.split("/")
        if (card_state(card) not in ("proposed", "done", "rejected") and card.get("claimant") == self.swarm
                and len(parts) > 1 and parts[1] not in ("-", self.node)):
            raise ValueError(f"card {ref} is held by {owner}. Ask Tony to drag it back to Proposed, "
                             "then claim it again")

    async def _claimed(self, who: Who, task: dict, card: dict) -> dict:
        """The claim's answer: the task, and where to work with whom."""
        where = await self._where(who, card)
        return await self._result(task["id"], ref=short_ref(card["id"]), card_id=card["id"],
                                  card_state=task["fed_state"] or card_state(card),
                                  wakes_on_messages=who.kind == "bg", **where)

    async def _where(self, who: Who, card: dict) -> dict:
        """The scope to work in for a card, whether it exists, and who is already working there."""
        project = card.get("project") or None
        scope = card.get("scope") or None
        out: dict[str, Any] = {"project": project, "scope": scope, "scope_source": "label" if scope else None,
                               "scope_exists": None, "worktree": None, "project_scopes": [],
                               "sessions": None}
        if not project:
            out["next"] = ("The card names no project. Ask Tony which scope to work in, or find one "
                           "with scope search.")
            return out
        root = config.projects_dir() / project
        out["project_scopes"] = await asyncio.to_thread(_scopes_of, root)
        if not scope:
            scope = out["scope"] = f"card-{short_ref(card['id'])}"
            out["scope_source"] = "suggested"
        path = root / scope
        exists = await asyncio.to_thread(path.is_dir)
        out.update(scope_exists=exists, worktree=str(path) if exists else None)
        try:
            rows = await self.cx.list()
        except CxUnavailable as exc:
            out["sessions_error"] = str(exc)
            rows = []
        else:
            out["sessions"] = [
                {"job": r.get("job"), "name": r.get("name"), "state": r.get("state"), "node": r.get("node")}
                for r in rows if r.get("project") == project and r.get("scope") == scope
                and r.get("job") != who.key and r.get("state") not in (GONE, STOPPED)]
        if exists:
            others = out["sessions"] or []
            out["next"] = (f"Work in {path}." + (
                f" {len(others)} session(s) already work there: tell them what you take on "
                "(reflection message or SendMessage) before you edit." if others else ""))
        else:
            out["next"] = (f"Scope {project}/{scope} does not exist. Run scope create for it, "
                           "then work in its worktree.")
        return out

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
            await asyncio.to_thread(_touch_marker, blocked_marker(who.session_id))
        if urgent and not already_urgent:
            out["notified"] = await self._notify(await self.db.get(task["id"]), reminder=False)
        return out

    async def _review(self, who: Who, args: dict) -> dict:
        summary = str(args.get("summary") or args.get("reason") or "").strip()
        if not summary:
            raise ValueError("summary is required")
        task = await self._task(who, REVIEW)
        return await self._set(task, REVIEW, "review", summary, reason=summary, urgent=0)

    # -- watching cards --------------------------------------------------------

    async def board_watch(self, who: Who, args: dict) -> dict:
        job = self._watcher_job(who)
        targets = []
        if args.get("card_id"):
            targets.append(await self._card_id(str(args["card_id"])))
        if args.get("posted_by_me") is True:
            targets.append(f"posted:{self._posting_name(who)}")
        if not targets:
            raise ValueError("pass card_id or posted_by_me=true")
        for target in targets:
            await self.qdb.watch(job, target)
        out = {"ok": True, "watching": await self.qdb.watched_by(job)}
        if config.refusal() is not None:
            out["warning"] = ("this node does not run the front door, so card events reach this "
                              "session only if the front door runs here")
        return out

    async def board_unwatch(self, who: Who, args: dict) -> dict:
        job = self._watcher_job(who)
        targets: list[str | None] = []
        if args.get("card_id"):
            targets.append(await self._card_id(str(args["card_id"])))
        if args.get("posted_by_me") is True:
            targets.append(f"posted:{self._posting_name(who)}")
        for target in targets or [None]:
            await self.qdb.unwatch(job, target)
        return {"ok": True, "watching": await self.qdb.watched_by(job)}

    @staticmethod
    def _watcher_job(who: Who) -> str:
        if who.kind != "bg":
            raise ValueError("board_watch types events into a background session; this session "
                             "has no job id to type into")
        return who.key

    def _posting_name(self, who: Who) -> str:
        name = self._agent_name(who.pid)
        if not name:
            raise ValueError("this session has no name, so the board cannot tell its posts apart; "
                             "pass card_id instead")
        return name

    async def _card_id(self, ref: str) -> str:
        card = await self.fed.resolve(ref)
        if card is None:
            raise ValueError(f"no card matches {ref}, or this swarm may not see it")
        return card["id"]

    async def card_closed(self, card_id: str, state: str) -> None:
        """The federation card finished on the board: close the task that holds it."""
        async with self.lock:
            task = await self.db.open_for_card(card_id)
            if task is not None:
                await self.close(task, DONE if state == "done" else REJECTED, fed_state=state)

    # -- conversion ----------------------------------------------------------

    async def convert(self, dry_run: bool = True) -> dict:
        """Bring this node's open cards to the current shape and stamp the Ops notices that lack a time."""
        async with self.lock:
            cards = []
            for task in await self.db.open(self.node):
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
        """One pass over this node's tasks: flag the ended, follow Tony's board edits, retry the board.

        ``rows`` are the local ``reflection list_other`` rows. A background job with no row on
        two passes in a row has left the daemon's roster.
        """
        async with self.lock:
            jobs = {r.get("job"): r for r in rows}
            for task in await self.db.open(self.node):
                row = jobs.get(task["key"]) if task["kind"] == "bg" else None
                if row:
                    await self._follow(task, row.get("cwd"))
                await self._ending(task, jobs.get(task["key"]))
            await self._workers(rows)
            await self._board()
            for task in await self.db.open(self.node):
                if task["kind"] == "bg":
                    await self._agent(task, jobs.get(task["key"]))
            await self._finished_cards()
            await self._archive()
            await self._catch_up()
            for task in await self.db.open(self.node):
                if (task["state"] == BLOCKED and task["urgent"] and not task["reminded"]
                        and time.time() - (task["blocked_at"] or 0) >= config.remind_s()
                        and await self._notify(task, reminder=True)):
                    await self.db.update(task["id"], reminded=1)

    async def _agent(self, task: dict, row: dict | None) -> None:
        """Record the live session working the task, for its card's Agent line.

        The link is the session's claude.ai page, or the attach command while
        Remote Control is off. A rename, a Remote Control toggle or the session
        stopping rewrites the card once. A sweep that misses the job's row
        changes nothing, since the task's close drops the line anyway. Only the
        card's body is rewritten, never its column, so a drag or a removal by
        Tony stands. A failed write is tried again on the next sweep.
        """
        if row is None:
            return
        if row.get("state") not in (GONE, STOPPED):
            agent = row.get("name") or task["key"]
            link = (row.get("link") if row.get("remote_control") else None) or row.get("attach")
        else:
            agent = link = None
        if (agent, link) == (task.get("agent"), task.get("agent_link")) or not task["note_id"]:
            return
        await self.db.update(task["id"], agent=agent, agent_link=link)
        try:
            await self._write_card(await self.db.get(task["id"]))
        except BoardError as exc:
            log.warning("reporting: task %s waits for the board: %s", task["id"], exc)
            await self.db.update(task["id"], agent=task.get("agent"), agent_link=task.get("agent_link"))

    async def _workers(self, rows: list[dict]) -> None:
        """Open a card in Active for each running worker cx started with the brief.

        Only a job the ledger has never held gets one, so a card Tony closed
        stays closed. Sessions started before the brief carry no ``brief``, and a
        kept seat never gets it.
        """
        for row in rows:
            job = row.get("job")
            if (not job or not row.get("brief") or row.get("pinned")
                    or row.get("state") in (GONE, STOPPED)):
                continue
            if await self.db.find(self.node, job) is None:
                await self._open(Who(job, "bg", None, "", row.get("cwd")), ACTIVE)

    async def _ending(self, task: dict, row: dict | None) -> None:
        """Flag a task whose session stopped running. Only a deliberate stop moves its card.

        A background job missing from cx whose directory remains was dropped from
        the daemon's roster (an idle retire, a daemon death) and can be resumed, so
        its card keeps its column. A stopped job, or one whose directory is gone,
        moves to Pending review. The sweep never closes a background task.
        """
        if task["kind"] == "bg":
            if row is None:
                self._missing[task["id"]] = self._missing.get(task["id"], 0) + 1
                if self._missing[task["id"]] < 2:
                    return
            self._missing.pop(task["id"], None)
            state = row.get("state") if row else None
            if state == GONE or (row is None and await asyncio.to_thread(self._job_exists, task["key"])):
                if not task["ended"]:
                    await self.db.update(task["id"], ended=1, dirty=1)
                    await self.db.event(task["id"], "ended", NOT_RUNNING)
                return
            if state not in (None, STOPPED):
                if task["ended"]:
                    await self.db.update(task["id"], ended=0, dirty=1)
                    await self.db.event(task["id"], "resumed")
                return
            if task["state"] != REVIEW:
                # Unfinished work is not reported to a federation card's poster.
                await self._set(task, REVIEW, "ended", STOPPED_DETAIL, ended=1, fed_state=None)
            elif not task["ended"]:
                await self.db.update(task["id"], ended=1, dirty=1)
                await self.db.event(task["id"], "ended", STOPPED_DETAIL)
            return
        if await asyncio.to_thread(self._alive, task["pid"], task["key"].split(":", 1)[1]):
            return
        if task["state"] == REVIEW:
            # The work waits for Tony, who closes it by dragging the card to Done.
            if not task["ended"]:
                await self.db.update(task["id"], ended=1, dirty=1)
                await self.db.event(task["id"], "ended", "the session ended in review")
            return
        await self.close(task, fed_state=None)

    async def _board(self) -> None:
        try:
            snap = await self.board.snapshot()
        except BoardError as exc:
            log.warning("reporting: cannot read the Swarm board: %s", exc)
            snap = None
        state_of = {name: state for state, name in COLUMNS.items()}
        for task in await self.db.open(self.node):
            column = snap.get(task["note_id"]) if snap is not None else None
            if column is not None:
                self._unseen.discard(task["id"])
            if snap is not None and task["note_id"] and not task["dirty"] and column != task["shown"]:
                if column is None and task["id"] not in self._unseen:
                    self._unseen.add(task["id"])  # one sweep can read mid-drag; cancel on the second
                elif column is None:
                    self._unseen.discard(task["id"])
                    await self.db.update(task["id"], dirty=1)
                    await self.db.close(task["id"], CANCELLED)
                else:
                    await self.db.event(task["id"], "dragged", f"{task['shown']} -> {column}")
                    try:
                        await self.board.stamp(task["note_id"])
                    except BoardError as exc:
                        log.warning("reporting: cannot date the drag of card %s: %s", task["note_id"], exc)
                    if state_of[column] in (DONE, REJECTED):
                        await self.close(task, state_of[column], shown=column)
                    else:
                        await self.db.update(task["id"], state=state_of[column], shown=column, dirty=1)
            if (await self.db.get(task["id"]))["dirty"]:
                await self.sync(task["id"])
        for task in await self.db.closed(self.node):
            if task["dirty"]:
                await self.sync(task["id"])

    async def _finished_cards(self) -> None:
        """Close the tasks whose federation card finished, for a node that follows no stream."""
        now = time.time()
        for task in await self.db.open(self.node):
            if (not task["card_id"] or task["fed_state"] is None or task["dirty"]
                    or now - self._fed_checked.get(task["id"], 0.0) < config.catchup_interval_s()):
                continue
            self._fed_checked[task["id"]] = now
            try:
                state = card_state(await self.fed.get(task["card_id"]))
            except FedError:
                continue
            if state in ("done", "rejected") and state != task["fed_state"]:
                await self.close(task, DONE if state == "done" else REJECTED, fed_state=state)

    async def _archive(self) -> None:
        """Fold the cards that sat in Done or Rejected for two weeks into the column's Older book."""
        try:
            for task in await self.db.archivable(self.node):
                if await self.board.fold(task["note_id"], COLUMNS[task["state"]]):
                    await self.db.update(task["id"], shown=OLDER_SHOWN)
            await self.board.count_older()
        except BoardError as exc:
            log.warning("reporting: cannot fold the finished cards: %s", exc)

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
        session = str(meta.get("session") or "")
        for task in await self.db.open(self.node):
            if (await self._posted_by(task, post, session)
                    and _posted_at(post) >= task["started"]
                    and await self.db.event(task["id"], "journal", f"{title}\n\n{body}" if title else body,
                                            ref=str(post["id"]))):
                await self.db.update(task["id"], dirty=1)
                await self.sync(task["id"])

    async def _posted_by(self, task: dict, post: dict, session: str) -> bool:
        """Whether ``post`` belongs on ``task``: the posting session's live card when
        the post names its session, else every card in the post's scope."""
        if session:
            if "/" in task["key"]:  # filed away: the session has moved on to another card
                return False
            return session == task["session_id"] or session in await self.db.aliases(task["id"])
        return (task["project"], task["scope"]) == (post.get("project"), post.get("scope"))

    async def _catch_up(self) -> None:
        """Read the recent journal of every open task's scope, for posts the subscription missed."""
        if time.time() - self._caught_up < config.catchup_interval_s():
            return
        self._caught_up = time.time()
        for project, scope in {(t["project"], t["scope"]) for t in await self.db.open(self.node)
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
