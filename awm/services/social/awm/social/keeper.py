"""Kept seats: social starts the holder of each ``[seat.*]`` and keeps it alive.

The agent is an ordinary background session that cx starts on the seat's node,
pinned so the daemon's idle retirement passes it over. The keeper writes the
mount row itself, so the seat's messages reach the agent through the same
reflection push as any mounted seat. Nothing in the agent watches the channel.

A tick asks cx on each seat's node which sessions exist, and does nothing for a
node that does not answer, because starting on an unknown state is how
duplicates happen. For each seat, with its recorded job:

* alive: nothing, except that an outdated launch spec is replaced once the
  session sits between turns.
* exited with its launch spec unchanged: resume it, so it keeps its own
  conversation and its cache, and push it one line naming what it missed.
  Repeated resumes back off, and after `MAX_RESUMES` without settling the
  keeper starts a new agent instead.
* missing, failed on an API error, or not resumable: start a new one. Repeated
  replacements back off from `MIN_RETRY_S` to `MAX_RETRY_S`, and the count
  resets once an agent has run for `SETTLE_S`.
* blocked on a login or a usage limit: a warning when the state changes. A
  restart cannot answer a login, so the keeper leaves it for a person.

A start that outlived its call leaves a live job the keeper has no record of.
It is adopted when it carries the seat's session name and no parent session.

The keeper holds a non-blocking flock for the life of the process, so a second
copy of social cannot start a second agent for a seat.
"""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable

from awm import config, gatewayclient
from awm.social.config import SeatConfig

log = logging.getLogger("awm.social.keeper")

TICK_S = 60.0
MIN_RETRY_S = 60.0
MAX_RETRY_S = 900.0
SETTLE_S = 600.0
QUIET_S = 600.0
START_TIMEOUT_S = 240.0
RESUME_TIMEOUT_S = 120.0
MAX_RESUMES = 3
LIST_TIMEOUT_S = 30.0
STOP_TIMEOUT_S = 60.0

GONE, STOPPED = "gone", "stopped"
BLOCKED, FAILED, WAITING = "blocked", "failed", "waiting"
ACTIVE, WORKING = "active", "working"
#: The `needs` Claude Code writes for an API error it cannot classify. A new
#: session is the only mend.
API_ERROR = "API error"
#: Starts of the `needs` texts that a person must clear.
#: How far below its `--autocompact` window Claude Code 2.1.301 compacts: a 20k
#: reserve for output plus a 13k buffer for the summary. A seat that should
#: compact at `compact_at` gets a window this much larger.
AUTOCOMPACT_RESERVE = 33_000
PERSON_NEEDS = (
    "login required", "org disabled oauth", "account on hold",
    "cloud credentials unavailable", "organization verification required",
    "usage limit reached",
)


def session_name(seat: SeatConfig) -> str:
    return f"seat - {seat.name}"


def spec_hash(spec: dict) -> str:
    return hashlib.sha256(json.dumps(spec, sort_keys=True, default=str).encode()).hexdigest()[:16]


def launch_spec(seat: SeatConfig, platform: str, keeps_history: bool) -> dict:
    """The `cx start` arguments of a seat's agent.

    Everything the seat must keep sits in the system prompt, which a compaction
    never rewrites. The prompt is only the first turn's instruction.
    """
    where = f"account={seat.account} channel={seat.channel}"
    backlog = "history" if keeps_history else "fetch"
    system_prompt = "\n\n".join([
        f"You hold the kept seat {seat.name!r}: a {platform} chat ({where}). "
        "Social keeps this session alive. It resumes this conversation when the "
        "session ends, and starts a new one only when this brief changes.",
        seat.brief,
        f"Each new message in the chat is typed into your prompt as one line: "
        f"'{platform} from <sender> [channel=<id> message_id=<id>]: <text>'. A "
        "thread reply carries ' ↳<thread>' after the sender, and a bot sender "
        "carries ' [bot]'. Nothing else delivers them, so do not run a Monitor "
        "or poll.",
        f"Reply with `social send {where} text=<text>`. Add thread=<thread> to "
        f"answer in a thread. Read the backlog with `social {backlog} {where}`.",
        f"Before work that takes a while, react 👀 with `social react {where} "
        "message_id=<id> emoji=👀`, then post the answer with `social send`.",
        f"Share a long document, such as a plan, as a file with `social send "
        f"{where} file=<path> text=<summary>` instead of pasting it.",
        "When this conversation is compacted, the summary must carry every "
        "question still unanswered, every agent you dispatched and what it is "
        "for, and everything you promised someone.",
    ])
    prompt = ("Start by reading the backlog, then answer what is still unanswered. "
              "Then wait for the next pushed line.")
    spec: dict[str, Any] = {
        "project": seat.project, "scope": seat.scope, "name": session_name(seat),
        "system_prompt": system_prompt, "prompt": prompt,
        "autocompact": seat.compact_at + AUTOCOMPACT_RESERVE,
        "remote_control": True, "pin": True,
    }
    if seat.model:
        spec["model"] = seat.model
    if seat.effort:
        spec["effort"] = seat.effort
    return spec


def catch_up_line(seat: SeatConfig, keeps_history: bool, since: float | None) -> str:
    """The line a resumed seat gets: lines pushed while it was down were dropped."""
    where = f"account={seat.account} channel={seat.channel}"
    if keeps_history and since:
        read = f"`social history {where} since={since:.0f}`"
    else:
        read = f"`social {'history' if keeps_history else 'fetch'} {where}`"
    return (f"social keeper: this session stopped and was resumed. Lines that "
            f"arrived meanwhile were not pushed to you. Read them with {read}, and "
            "answer only what is still unanswered.")


# -- health -------------------------------------------------------------------

@dataclass(frozen=True)
class Health:
    """``state`` is running, waiting, blocked, failed, exited or missing."""

    state: str
    reason: str = ""
    job: str | None = None

    @property
    def alive(self) -> bool:
        return self.state in ("running", WAITING)


def _seen_epoch(rec: dict) -> float | None:
    """When the keeper last saw the job alive, else when it started it."""
    try:
        return float(rec.get("seen_at") or "")
    except ValueError:
        pass
    try:
        return datetime.fromisoformat(rec.get("started_at") or "").timestamp()
    except ValueError:
        return None


def _short(text: Any, limit: int = 120) -> str:
    return " ".join(str(text or "").split())[:limit]


def classify(row: dict) -> Health:
    """Health of one live session from its `cx list` row.

    Claude Code also sets `blocked` and `failed` from the session's own last
    line, so only the harness's own `needs` texts count. An unfamiliar state
    counts as running, so it never starts a restart.
    """
    job = row.get("job")
    state = row.get("state")
    needs = _short(row.get("needs"))
    if state == FAILED and needs == API_ERROR:
        return Health(FAILED, API_ERROR, job)
    if state != BLOCKED:
        return Health("running", "", job)
    lowered = needs.lower()
    if lowered.startswith("login required") or "login expired" in str(row.get("detail") or "").lower():
        return Health(BLOCKED, "auth_required", job)
    if lowered.startswith(PERSON_NEEDS):
        return Health(BLOCKED, needs, job)
    return Health(WAITING, needs or "needs input", job)


def _live(row: dict) -> bool:
    return row.get("state") not in (GONE, STOPPED)


def _mid_turn(row: dict) -> bool:
    tempo = row.get("tempo")
    return tempo == ACTIVE if tempo else row.get("state") == WORKING


# -- cx on the seat's node ----------------------------------------------------

class CxUnavailable(RuntimeError):
    """cx did not answer, so nothing can be said about the node's sessions."""


class Cx:
    """`cx list`, `start`, `resume` and `stop` on a node, local or a domestic peer."""

    async def _call(self, node: str, fn: str, args: dict, timeout: float) -> Any:
        if node == config.node_name():
            return await gatewayclient.call("cx", fn, args, timeout=timeout)
        return await gatewayclient.call_peer(node, "cx", fn, args, timeout=timeout)

    async def list(self, node: str) -> list[dict]:
        try:
            reply = await self._call(node, "list", {}, LIST_TIMEOUT_S)
        except Exception as exc:  # noqa: BLE001 — any failure means "cannot tell"
            raise CxUnavailable(f"cx list on {node} failed: {exc}") from exc
        if not isinstance(reply, dict) or "sessions" not in reply:
            reason = reply.get("reason") if isinstance(reply, dict) else None
            raise CxUnavailable(f"cx list on {node} refused: {reason or reply!r}")
        return [row for row in reply["sessions"] if isinstance(row, dict)]

    async def start(self, node: str, spec: dict) -> dict:
        try:
            reply = await self._call(node, "start", spec, START_TIMEOUT_S)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "reason": f"cx start on {node} failed: {exc}"}
        return reply if isinstance(reply, dict) else {"ok": False, "reason": repr(reply)}

    async def resume(self, node: str, job: str) -> dict:
        try:
            reply = await self._call(node, "resume", {"job": job}, RESUME_TIMEOUT_S)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "reason": f"cx resume on {node} failed: {exc}"}
        return reply if isinstance(reply, dict) else {"ok": False, "reason": repr(reply)}

    async def stop(self, node: str, job: str) -> dict:
        try:
            reply = await self._call(node, "stop", {"job": job}, STOP_TIMEOUT_S)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "reason": f"cx stop on {node} failed: {exc}"}
        return reply if isinstance(reply, dict) else {"ok": False, "reason": repr(reply)}


# -- the loop -----------------------------------------------------------------

#: (account) -> (platform, keeps_history), or None when the account is not connected here.
Describe = Callable[[str], "tuple[str, bool] | None"]
#: (account, channel id) -> the chat's name.
ChannelName = Callable[[str, str], Awaitable[str]]
#: (node, job, line) -> queue one line for a session's prompt.
Push = Callable[[str, str, str], None]


class Keeper:
    def __init__(self, dao: Any, seats: list[SeatConfig], describe: Describe,
                 channel_name: ChannelName, cx: Any = None,
                 interval_s: float = TICK_S, lock_path: Path | None = None,
                 clock: Callable[[], float] = time.time,
                 push: Push | None = None) -> None:
        self.dao = dao
        self.seats = list(seats)
        self._describe = describe
        self._channel_name = channel_name
        self.cx = cx or Cx()
        self._push = push
        self._interval_s = interval_s
        self._lock_path = lock_path
        self._lock_fd: int | None = None
        self._now = clock
        self._notes: dict[str, float] = {}
        self._next_try: dict[str, float] = {}
        self._failures: dict[str, int] = {}
        self._restarts: dict[str, int] = {}
        self._restarted_at: dict[str, float] = {}
        self._resumes: dict[str, int] = {}
        self._resumed_at: dict[str, float] = {}
        self._seen: dict[str, tuple] = {}
        self.health: dict[str, Health] = {}
        self._last_tick: float | None = None

    def kept(self, account: str, channel: str) -> SeatConfig | None:
        return next((s for s in self.seats
                     if s.account == account and s.channel == channel), None)

    def status(self, seat: SeatConfig) -> dict:
        rec = self.dao.keeper(seat.name) or {}
        health = self.health.get(seat.name)
        return {"seat": seat.name, "node": seat.node, "job": rec.get("job"),
                "started_at": rec.get("started_at"),
                "health": health.state if health else "unknown",
                "reason": health.reason if health else "",
                "restarts": self._restarts.get(seat.name, 0),
                "resumes": self._resumes.get(seat.name, 0)}

    async def run(self) -> None:
        """Tick, then sleep, forever. Never returns, never raises."""
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — a tick must never end the loop
                log.exception("keeper: tick failed")
            await asyncio.sleep(self._interval_s)

    async def tick(self) -> None:
        if not self.seats or not await asyncio.to_thread(self._take_lock):
            return
        rows_by_node: dict[str, list[dict] | None] = {}
        for seat in self.seats:
            if seat.node not in rows_by_node:
                try:
                    rows_by_node[seat.node] = await self.cx.list(seat.node)
                except CxUnavailable as exc:
                    self._note(f"cx:{seat.node}", f"not keeping seats on {seat.node}: {exc}")
                    rows_by_node[seat.node] = None
            rows = rows_by_node[seat.node]
            if rows is not None:
                await self._keep(seat, rows)
        self._last_tick = self._now()

    async def _keep(self, seat: SeatConfig, rows: list[dict]) -> None:
        described = self._describe(seat.account)
        if described is None:
            self._note(f"account:{seat.name}",
                       f"{seat.name}: account {seat.account!r} is not connected here")
            return
        spec = launch_spec(seat, *described)
        want = spec_hash({**spec, "node": seat.node})
        rec = self.dao.keeper(seat.name)
        if rec and rec["node"] != seat.node:
            rec = None
        row = next((r for r in rows if rec and r.get("job") == rec["job"] and _live(r)), None)
        if row is None:
            adopted = self._adoptable(seat, rows, rec)
            if adopted is not None:
                self.dao.set_keeper(seat.name, seat.node, adopted["job"], want)
                log.info("keeper: adopted %s for %s; its start outlived the call",
                         adopted["job"], seat.name)
                rec, row = self.dao.keeper(seat.name), adopted
        if row is not None:
            health = classify(row)
            self._report(seat, health)
            if health.alive:
                if rec["spec_hash"] != want:
                    if _mid_turn(row):
                        self._note(f"outdated:{seat.name}", f"{seat.name}: {row['job']} "
                                   "runs an outdated spec; replacing it after its turn")
                    else:
                        await self._replace(seat, spec, want, rows, old=row["job"],
                                            why="its launch spec changed", counted=False)
                        return
                await self._hold(seat, row["job"])
                self.dao.keeper_seen(seat.name, self._now())
                self._settle(seat.name)
                return
            if health.state == BLOCKED:
                return
            await self._replace(seat, spec, want, rows, old=row["job"],
                                why=f"it failed ({health.reason})")
            return
        health = Health("exited" if rec else "missing", "", rec["job"] if rec else None)
        self._report(seat, health)
        if rec and rec["spec_hash"] == want and await self._resume(seat, rec, rows, described[1]):
            return
        await self._replace(seat, spec, want, rows, old=rec["job"] if rec else None,
                            why="it is not running" if rec else "it has none yet",
                            counted=rec is not None)

    def _adoptable(self, seat: SeatConfig, rows: list[dict], rec: dict | None) -> dict | None:
        return next((r for r in rows if _live(r) and r.get("job")
                     and r.get("name") == session_name(seat)
                     and r.get("parent") is None
                     and (rec is None or r["job"] != rec["job"])), None)

    async def _resume(self, seat: SeatConfig, rec: dict, rows: list[dict],
                      keeps_history: bool) -> bool:
        """Bring the recorded job back. False means start a new agent instead."""
        now, job = self._now(), rec["job"]
        if self._next_try.get(seat.name, 0.0) > now:
            return True
        resumes = self._resumes.get(seat.name, 0)
        if resumes >= MAX_RESUMES:
            self._note(f"resume:{seat.name}", f"{seat.name}: {job} stopped again after "
                       f"{resumes} resumes; starting a new agent")
            self._resumes.pop(seat.name, None)
            self._resumed_at.pop(seat.name, None)
            return False
        taken = [r for r in rows if r.get("name") == session_name(seat) and _live(r)
                 and r.get("job") != job]
        if taken:
            self._note(f"name:{seat.name}", f"{seat.name}: session {taken[0].get('job')} "
                       f"already holds the name {session_name(seat)!r}; not resuming")
            return True
        reply = await self.cx.resume(seat.node, job)
        if not reply.get("ok"):
            self._note(f"resume:{seat.name}", f"{seat.name}: cx would not resume {job}: "
                       f"{reply.get('reason') or reply!r}; starting a new agent")
            self._resumes.pop(seat.name, None)
            self._resumed_at.pop(seat.name, None)
            return False
        self._resumes[seat.name] = resumes + 1
        self._resumed_at[seat.name] = now
        self._next_try[seat.name] = now + self._backoff(resumes + 1)
        await self._hold(seat, job)
        self.health[seat.name] = Health("running", "", job)
        if self._push:
            self._push(seat.node, job, catch_up_line(seat, keeps_history, _seen_epoch(rec)))
        log.info("keeper: resumed %s for %s on %s", job, seat.name, seat.node)
        return True

    async def _replace(self, seat: SeatConfig, spec: dict, want: str, rows: list[dict],
                       old: str | None, why: str, counted: bool = True) -> None:
        now = self._now()
        if self._next_try.get(seat.name, 0.0) > now:
            return
        taken = [r for r in rows if r.get("name") == spec["name"] and _live(r)
                 and r.get("job") != old]
        if taken:
            self._note(f"name:{seat.name}", f"{seat.name}: session {taken[0].get('job')} "
                       f"already holds the name {spec['name']!r}; not starting")
            return
        if counted:
            restarts = self._restarts[seat.name] = self._restarts.get(seat.name, 0) + 1
            self._restarted_at[seat.name] = now
            self._next_try[seat.name] = now + self._backoff(restarts)
        if old:
            reply = await self.cx.stop(seat.node, old)
            if not reply.get("ok"):
                log.info("keeper: %s: cx stop %s answered %s", seat.name, old,
                         reply.get("reason") or reply)
        reply = await self.cx.start(seat.node, spec)
        if not (reply.get("ok") and reply.get("job")):
            failures = self._failures[seat.name] = self._failures.get(seat.name, 0) + 1
            self._next_try[seat.name] = max(self._next_try.get(seat.name, 0.0),
                                            now + self._backoff(failures))
            self._note(f"start:{seat.name}", f"{seat.name}: cx would not start it: "
                       f"{reply.get('reason') or reply.get('error') or reply!r}")
            return
        self._failures.pop(seat.name, None)
        job = reply["job"]
        self.dao.set_keeper(seat.name, seat.node, job, want)
        await self._hold(seat, job)
        self.health[seat.name] = Health("running", "", job)
        log.info("keeper: started %s for %s on %s (%s)", job, seat.name, seat.node, why)

    async def _hold(self, seat: SeatConfig, job: str) -> None:
        """Make ``job`` the seat's holder, unless it already is."""
        held = self.dao.holder(seat.account, seat.channel)
        if held and (held["node"], held["job"]) == (seat.node, job):
            return
        try:
            name = await self._channel_name(seat.account, seat.channel)
        except Exception:  # noqa: BLE001 — the id still routes the push
            name = (held or {}).get("channel_name") or ""
        self.dao.mount(seat.account, seat.channel, name,
                       {"node": seat.node, "job": job, "name": session_name(seat)})

    def _backoff(self, n: int) -> float:
        base = max(MIN_RETRY_S, self._interval_s)
        return min(MAX_RETRY_S, base * 2 ** (n - 1))

    def _settle(self, seat: str) -> None:
        since = self._restarted_at.get(seat)
        if since is not None and self._now() - since >= SETTLE_S:
            self._restarts.pop(seat, None)
            self._restarted_at.pop(seat, None)
            self._next_try.pop(seat, None)
        since = self._resumed_at.get(seat)
        if since is not None and self._now() - since >= SETTLE_S:
            self._resumes.pop(seat, None)
            self._resumed_at.pop(seat, None)
            if seat not in self._restarted_at:
                self._next_try.pop(seat, None)

    def _report(self, seat: SeatConfig, health: Health) -> None:
        self.health[seat.name] = health
        seen = (health.job, health.state, health.reason)
        before = self._seen.get(seat.name)
        if before == seen:
            return
        self._seen[seat.name] = seen
        if health.state in (BLOCKED, FAILED):
            log.warning("keeper: %s (%s) is %s: %s", seat.name, health.job,
                        health.state, health.reason or "no reason given")

    def _note(self, key: str, message: str) -> None:
        last = self._notes.get(key)
        if last is not None and self._now() - last < QUIET_S:
            return
        log.info("keeper: %s", message)
        self._notes[key] = self._now()

    def _take_lock(self) -> bool:
        """Hold the keeper lock for the life of the process."""
        if self._lock_fd is not None:
            return True
        path = self._lock_path or (config.AWM_DIR / "services" / "social" / "keeper.lock")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        except OSError as exc:
            self._note("lock", f"cannot open the keeper lock: {exc}")
            return False
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            self._note("lock", "another social holds the keeper lock; not keeping seats")
            return False
        self._lock_fd = fd
        log.info("keeper: holding the keeper lock")
        return True
