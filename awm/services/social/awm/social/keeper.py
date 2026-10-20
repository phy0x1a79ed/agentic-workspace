"""Kept seats: social starts the holder of each ``[seat.*]`` and keeps it alive.

The agent is an ordinary background session that cx starts on the seat's node,
pinned so the daemon's idle retirement passes it over. The keeper writes the
mount rows itself, so the seat's messages reach the agent through the same
reflection push as any mounted seat. Nothing in the agent watches the channel.
A seat holds one chat, a list of chats, or every chat of its account, and the
keeper mounts a chat the account joins on the next tick.

A tick asks cx on each seat's node which sessions exist and whether the node's
Claude Code daemon runs. It does nothing for a node that does not answer,
because starting on an unknown state is how duplicates happen. A seat whose
agent is alive needs nothing, except that an outdated launch spec is replaced
once the session sits between turns. For any other seat `awm.kept.decide`
chooses:

* the daemon is down: wait. The keeper never stops or replaces a session on a
  node without a daemon, because a resume keeps the conversation once the
  daemon is back.
* exited with its launch spec unchanged: resume it, so it keeps its own
  conversation and its cache.
* missing, failed on an API error, not resumable, or resumed `MAX_RESUMES`
  times without settling: start a new agent that ``replaces`` the old job, so
  cx hands it the old conversation's last turns and then retires the old job.
* blocked on a login or a usage limit: a warning when the state changes. A
  restart cannot answer a login, so the keeper leaves it for a person.

Each resume or start counts against the seat's breaker, which backs off and,
past its budget, holds the seat as broken until ``keep_reset`` or until it
clears itself. The breaker and the seat's open restart record live in
``social_keeper_state``, so a restart of social keeps counting where it was.
The record becomes the one line the seat gets when it comes back: why it was
down, for how long, and how many messages arrived meanwhile. A seat that stayed
up through a restart of social gets a line naming what arrived while social
was down.

A seat down longer than its ``alert_after`` raises one message to the
operator's DM, and its recovery one more, at most once an hour per seat. Seats
that go down together share one message.

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
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

from awm import config, gatewayclient
from awm.kept import Action, Breaker, Outage, Situation, decide
from awm.kept.decide import MAX_RESUMES
from awm.social.config import SeatConfig
from awm.social.seats import session_name

log = logging.getLogger("awm.social.keeper")

__all__ = ["MAX_RESUMES", "Keeper", "launch_spec", "classify"]

TICK_S = 60.0
QUIET_S = 600.0
START_TIMEOUT_S = 240.0
RESUME_TIMEOUT_S = 120.0
LIST_TIMEOUT_S = 30.0
ADOPT_TIMEOUT_S = 30.0
#: The least time between two alerts about one seat.
ALERT_EVERY_S = 3600.0
#: How often a seat of every chat lists the account's chats again.
CHATS_EVERY_S = 300.0
#: A seat that came back by itself gets a catch-up line only after an outage
#: this long, or when messages arrived meanwhile.
BLIP_S = 2 * TICK_S

GONE, STOPPED = "gone", "stopped"
BLOCKED, FAILED, NEEDS_INPUT = "blocked", "failed", "needs_input"
WAITING, RESUMING, BROKEN = "waiting", "resuming", "broken"
ACTIVE, WORKING = "active", "working"
DAEMON_DOWN = "daemon_down"
#: The `needs` Claude Code writes for an API error it cannot classify. A new
#: session is the only mend.
API_ERROR = "API error"
#: How far below its `--autocompact` window Claude Code 2.1.301 compacts: a 20k
#: reserve for output plus a 13k buffer for the summary. A seat that should
#: compact at `compact_at` gets a window this much larger.
AUTOCOMPACT_RESERVE = 33_000
#: Starts of the `needs` texts that a person must clear.
PERSON_NEEDS = (
    "login required", "org disabled oauth", "account on hold",
    "cloud credentials unavailable", "organization verification required",
    "usage limit reached",
)


def legacy_name(seat: SeatConfig) -> str:
    """The name seats started before they were named for their chat carry."""
    return f"seat - {seat.name}"


def spec_hash(spec: dict) -> str:
    """The hash that decides a recycle. It leaves out the name, so a renamed chat keeps its agent."""
    kept = {k: v for k, v in spec.items() if k != "name"}
    return hashlib.sha256(json.dumps(kept, sort_keys=True, default=str).encode()).hexdigest()[:16]


def legacy_hash(spec: dict, seat: SeatConfig) -> str:
    """The hash recorded before :func:`spec_hash` left out the name."""
    full = {**spec, "name": legacy_name(seat)}
    return hashlib.sha256(json.dumps(full, sort_keys=True, default=str).encode()).hexdigest()[:16]


def _chats_text(seat: SeatConfig) -> str:
    if seat.every_chat:
        skip = f" except {', '.join(seat.except_chats)}" if seat.except_chats else ""
        return f"every chat of account {seat.account}{skip}"
    return f"the chats {', '.join(seat.channels)} of account {seat.account}"


def launch_spec(seat: SeatConfig, platform: str, keeps_history: bool, name: str) -> dict:
    """The `cx start` arguments of a seat's agent.

    Everything the seat must keep sits in the system prompt, which a compaction
    never rewrites. The prompt is only the first turn's instruction. A seat of
    many chats is described by its config, never by the chats it holds now, so
    a chat the account joins does not change the spec.
    """
    backlog = "history" if keeps_history else "fetch"
    if seat.single:
        where = f"account={seat.account} channel={seat.channel}"
        holds = f"a {platform} chat ({where})"
        reply = where
        read = where
    else:
        where = f"account={seat.account} channel=<id>"
        holds = f"{_chats_text(seat)} on {platform}"
        reply = where
        read = f"account={seat.account}" if keeps_history else where
    system_prompt = "\n\n".join([
        f"You hold the kept seat {seat.name!r}: {holds}. "
        "Social keeps this session alive. It resumes this conversation when the "
        "session ends, and starts a new one only when this brief changes.",
        seat.brief,
        f"Each new message in the chat is typed into your prompt as one line: "
        f"'{platform} from <sender> [channel=<id> message_id=<id>]: <text>'. A "
        "thread reply carries ' ↳<thread>' after the sender, and a bot sender "
        "carries ' [bot]'. Nothing else delivers them, so do not run a Monitor "
        "or poll.",
        f"Reply with `social send {reply} text=<text>`. Add thread=<thread> to "
        f"answer in a thread. Read the backlog with `social {backlog} {read}`.",
        f"Before work that takes a while, react 👀 with `social react {reply} "
        "message_id=<id> emoji=👀`, then post the answer with `social send`.",
        f"Share a long document, such as a plan, as a file with `social send "
        f"{reply} file=<path> text=<summary>` instead of pasting it.",
        "When this conversation is compacted, the summary must carry every "
        "question still unanswered, every agent you dispatched and what it is "
        "for, and everything you promised someone.",
    ])
    prompt = ("Start by reading the backlog, then answer what is still unanswered. "
              "Then wait for the next pushed line.")
    spec: dict[str, Any] = {
        "project": seat.project, "scope": seat.scope, "name": name,
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
    """How a seat reads the lines that were not pushed to it since ``since``."""
    where = (f"account={seat.account} channel={seat.channel}" if seat.single
             else f"account={seat.account}")
    if keeps_history and since:
        read = f"`social history {where} since={since:.0f}`"
    elif keeps_history:
        read = f"`social history {where}`"
    else:
        read = f"`social fetch {where if seat.single else where + ' channel=<id>'}`"
    return (f"Lines that arrived meanwhile were not pushed to you. Read them with {read}, "
            "and answer only what is still unanswered.")


# -- health -------------------------------------------------------------------

@dataclass(frozen=True)
class Health:
    """``state`` is running, needs_input, blocked, failed, exited or missing,
    or what the keeper does about it: waiting, resuming or broken."""

    state: str
    reason: str = ""
    job: str | None = None

    @property
    def alive(self) -> bool:
        return self.state in ("running", NEEDS_INPUT)


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


def _epoch(iso: Any) -> float | None:
    if not isinstance(iso, str) or not iso:
        return None
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _iso(epoch: float | None) -> str | None:
    if epoch is None:
        return None
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat(timespec="seconds")


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
    return Health(NEEDS_INPUT, needs or "needs input", job)


def _live(row: dict) -> bool:
    return row.get("state") not in (GONE, STOPPED) and row.get("alive") is not False


def _mid_turn(row: dict) -> bool:
    tempo = row.get("tempo")
    return tempo == ACTIVE if tempo else row.get("state") == WORKING


def _daemon_up(daemon: dict | None) -> bool | None:
    state = (daemon or {}).get("state")
    return {"up": True, "down": False}.get(state)


def _span(seconds: float) -> str:
    minutes = max(0, round(seconds / 60))
    return f"{minutes} min" if minutes < 60 else f"{minutes // 60} h {minutes % 60} min"


def _hhmm(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%H:%MZ")


# -- what the keeper remembers about a seat ------------------------------------

@dataclass
class SeatState:
    """A seat's breaker, its open outage and the facts around them, persisted per seat."""

    breaker: Breaker = field(default_factory=Breaker)
    outage: Outage | None = None
    last_outage: dict | None = None
    alive_since: float | None = None
    #: A job cx refused to resume. The next attempt starts fresh.
    unresumable: str | None = None
    last_alert_at: float | None = None
    broken_alerted: bool = False

    def to_dict(self) -> dict:
        return {"breaker": self.breaker.to_dict(),
                "outage": self.outage.to_dict() if self.outage else None,
                "last_outage": self.last_outage, "alive_since": self.alive_since,
                "unresumable": self.unresumable, "last_alert_at": self.last_alert_at,
                "broken_alerted": self.broken_alerted}

    @classmethod
    def from_dict(cls, data: dict) -> "SeatState":
        return cls(breaker=Breaker.from_dict(data.get("breaker")),
                   outage=Outage.from_dict(data.get("outage")),
                   last_outage=data.get("last_outage"),
                   alive_since=data.get("alive_since"),
                   unresumable=data.get("unresumable"),
                   last_alert_at=data.get("last_alert_at"),
                   broken_alerted=bool(data.get("broken_alerted")))


# -- cx on the seat's node ----------------------------------------------------

class CxUnavailable(RuntimeError):
    """cx did not answer, so nothing can be said about the node's sessions."""


class Cx:
    """`cx list`, `start`, `resume` and `adopt` on a node, local or a domestic peer."""

    async def _call(self, node: str, fn: str, args: dict, timeout: float) -> Any:
        if node == config.node_name():
            return await gatewayclient.call("cx", fn, args, timeout=timeout)
        return await gatewayclient.call_peer(node, "cx", fn, args, timeout=timeout)

    async def list(self, node: str) -> tuple[list[dict], dict | None]:
        """The node's session rows, and its daemon's state when cx reports one."""
        try:
            reply = await self._call(node, "list", {}, LIST_TIMEOUT_S)
        except Exception as exc:  # noqa: BLE001 — any failure means "cannot tell"
            raise CxUnavailable(f"cx list on {node} failed: {exc}") from exc
        if not isinstance(reply, dict) or "sessions" not in reply:
            reason = reply.get("reason") if isinstance(reply, dict) else None
            raise CxUnavailable(f"cx list on {node} refused: {reason or reply!r}")
        daemon = reply.get("daemon")
        return ([row for row in reply["sessions"] if isinstance(row, dict)],
                daemon if isinstance(daemon, dict) else None)

    async def _verb(self, node: str, fn: str, args: dict, timeout: float) -> dict:
        try:
            reply = await self._call(node, fn, args, timeout)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "reason": f"cx {fn} on {node} failed: {exc}"}
        return reply if isinstance(reply, dict) else {"ok": False, "reason": repr(reply)}

    async def start(self, node: str, spec: dict) -> dict:
        return await self._verb(node, "start", spec, START_TIMEOUT_S)

    async def resume(self, node: str, job: str) -> dict:
        return await self._verb(node, "resume", {"job": job}, RESUME_TIMEOUT_S)

    async def adopt(self, node: str, job: str) -> dict:
        return await self._verb(node, "adopt", {"job": job}, ADOPT_TIMEOUT_S)


# -- the loop -----------------------------------------------------------------

#: (account) -> (platform, keeps_history), or None when the account is not connected here.
Describe = Callable[[str], "tuple[str, bool] | None"]
#: (account, channel id) -> the chat, with its ``name`` and ``kind``.
Chat = Callable[[str, str], Awaitable[Any]]
#: (account) -> every chat the account is in.
ListChats = Callable[[str], Awaitable[list]]
#: (node, job, line) -> queue one line for a session's prompt.
Push = Callable[[str, str, str], None]
#: (text) -> post one message to the operator.
Alert = Callable[[str], Awaitable[None]]


class Keeper:
    def __init__(self, dao: Any, seats: list[SeatConfig], describe: Describe,
                 chat: Chat, cx: Any = None,
                 interval_s: float = TICK_S, lock_path: Path | None = None,
                 clock: Callable[[], float] = time.time,
                 push: Push | None = None, list_chats: ListChats | None = None,
                 alert: Alert | None = None) -> None:
        self.dao = dao
        self.seats = list(seats)
        self._describe = describe
        self._chat = chat
        self._list_chats = list_chats
        self._names: dict[str, str] = {}
        self._chat_names: dict[str, str] = {}
        self._held: dict[str, list] = {}
        self._listed_at: dict[str, float] = {}
        self.cx = cx or Cx()
        self._push = push
        self._alert = alert
        self._interval_s = interval_s
        self._lock_path = lock_path
        self._lock_fd: int | None = None
        self._now = clock
        self._notes: dict[str, float] = {}
        self._seen: dict[str, tuple] = {}
        self._states: dict[str, SeatState] = {}
        self._saved: dict[str, dict] = {}
        self._booted_at: float | None = None
        self._boot_seen: dict[str, float] = {}
        self._recovered: list[tuple[SeatConfig, Outage]] = []
        self._broke: list[SeatConfig] = []
        self.health: dict[str, Health] = {}
        self._last_tick: float | None = None

    # -- the seats and their chats ----------------------------------------

    def kept(self, account: str, channel: str) -> SeatConfig | None:
        """The seat that keeps this chat. A seat naming the chat wins over one keeping every chat."""
        for seat in self.seats:
            if seat.account == account and (seat.channel == channel or channel in seat.channels):
                return seat
        return next((s for s in self.seats if s.every_chat and s.account == account
                     and channel not in s.except_chats), None)

    def _named_elsewhere(self, seat: SeatConfig) -> set[str]:
        return {c for s in self.seats if s is not seat and s.account == seat.account
                for c in (s.channels or (s.channel,)) if c}

    async def _chats(self, seat: SeatConfig) -> list:
        """The chats the seat holds now. A chat that cannot be read keeps its last known state."""
        try:
            if seat.every_chat:
                listed = self._listed_at.get(seat.name)
                if self._list_chats is None or (
                        listed is not None and self._now() - listed < CHATS_EVERY_S):
                    return self._held.get(seat.name, [])
                skip = set(seat.except_chats) | self._named_elsewhere(seat)
                chats = [c for c in await self._list_chats(seat.account) if c.id not in skip]
                self._listed_at[seat.name] = self._now()
            else:
                chats = [await self._chat(seat.account, c)
                         for c in (seat.channels or (seat.channel,))]
        except Exception as exc:  # noqa: BLE001 — a chat must not stop the keeper
            self._note(f"chat:{seat.name}", f"{seat.name}: cannot read its chats: {exc}")
            return self._held.get(seat.name, [])
        self._held[seat.name] = chats
        return chats

    async def _name(self, seat: SeatConfig, platform: str) -> str:
        """The seat's session name, read from its chat once per process."""
        if not seat.single:
            self._names[seat.name] = f"seat:{platform}/{seat.name}"
        if seat.name in self._names:
            return self._names[seat.name]
        try:
            chat = await self._chat(seat.account, seat.channel)
            self._names[seat.name] = session_name(platform, chat)
            self._chat_names[seat.name] = chat.name or ""
        except Exception as exc:  # noqa: BLE001 — a name must not stop the keeper
            self._note(f"chat:{seat.name}", f"{seat.name}: cannot read its chat: {exc}")
        return self._names.get(seat.name) or f"seat:{platform}/{seat.name}"

    def _state(self, seat: SeatConfig) -> SeatState:
        if seat.name not in self._states:
            data = self.dao.keeper_state(seat.name)
            self._states[seat.name] = SeatState.from_dict(data)
            self._saved[seat.name] = self._states[seat.name].to_dict()
        return self._states[seat.name]

    def _save(self, seat: SeatConfig) -> None:
        state = self._states.get(seat.name)
        if state is None:
            return
        data = state.to_dict()
        if data != self._saved.get(seat.name):
            self.dao.set_keeper_state(seat.name, data)
            self._saved[seat.name] = data

    # -- status and operator verbs ----------------------------------------

    def find(self, name: str) -> SeatConfig | None:
        return next((s for s in self.seats if s.name == name), None)

    def status(self, seat: SeatConfig) -> dict:
        rec = self.dao.keeper(seat.name) or {}
        health = self.health.get(seat.name)
        st = self._state(seat)
        now = self._now()
        outage = st.outage
        out = {"seat": seat.name, "node": seat.node, "job": rec.get("job"),
               "started_at": rec.get("started_at"),
               "health": health.state if health else "unknown",
               "reason": health.reason if health else "",
               "down_since": _iso(outage.down_at) if outage else None,
               "cause": outage.describe_cause() if outage else None,
               "attempts": st.breaker.recent(now),
               "resumes": st.breaker.recent(now, "resume"),
               "restarts": st.breaker.recent(now, "fresh"),
               "next_try": _iso(st.breaker.next_try) if st.breaker.next_try > now else None,
               "broken": st.breaker.tripped(now),
               "last_outage": st.last_outage}
        if not seat.single:
            out["chats"] = [c.id for c in self._held.get(seat.name, [])]
        return out

    def reset(self, seat: SeatConfig) -> dict:
        """Clear the seat's breaker, so the next tick tries again."""
        st = self._state(seat)
        st.breaker.clear()
        st.unresumable = None
        st.broken_alerted = False
        self._save(seat)
        log.info("keeper: %s: breaker cleared by an operator", seat.name)
        return self.status(seat)

    async def adopt(self, seat: SeatConfig, job: str) -> dict:
        """Make the running ``job`` the seat's agent, keeping its conversation."""
        described = self._describe(seat.account)
        if described is None:
            return {"ok": False, "reason": f"account {seat.account!r} is not connected here"}
        reply = await self.cx.adopt(seat.node, job)
        if not reply.get("ok") and "already has a lineage record" not in str(reply.get("reason")):
            return {"ok": False, "reason": reply.get("reason") or repr(reply)}
        spec = launch_spec(seat, *described, await self._name(seat, described[0]))
        self.dao.set_keeper(seat.name, seat.node, job, spec_hash({**spec, "node": seat.node}))
        self.dao.keeper_seen(seat.name, self._now())
        self._states[seat.name] = SeatState()
        self._save(seat)
        await self._hold(seat, job, await self._chats(seat))
        log.info("keeper: %s: adopted %s on %s", seat.name, job, seat.node)
        return {"ok": True, **self.status(seat)}

    # -- the tick ---------------------------------------------------------

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
        now = self._now()
        booting = self._booted_at is None
        if booting:
            self._booted_at = now
        listings: dict[str, tuple[list[dict], dict | None] | None] = {}
        for seat in self.seats:
            if seat.node not in listings:
                try:
                    listings[seat.node] = await self.cx.list(seat.node)
                except CxUnavailable as exc:
                    self._note(f"cx:{seat.node}", f"not keeping seats on {seat.node}: {exc}")
                    listings[seat.node] = None
            if booting:
                rec = self.dao.keeper(seat.name)
                seen = _seen_epoch(rec) if rec else None
                if seen is not None:
                    self._boot_seen[seat.name] = seen
            listing = listings[seat.node]
            if listing is None:
                self._unreachable(seat)
            else:
                await self._keep(seat, *listing)
            self._save(seat)
        await self._send_alerts()
        self._last_tick = now

    def _unreachable(self, seat: SeatConfig) -> None:
        rec = self.dao.keeper(seat.name)
        if rec is None:
            return
        st = self._state(seat)
        self._open(seat, st, rec, None, "its node did not answer")
        self._report(seat, Health(WAITING, "its node does not answer", rec["job"]))

    async def _keep(self, seat: SeatConfig, rows: list[dict], daemon: dict | None) -> None:
        described = self._describe(seat.account)
        if described is None:
            self._note(f"account:{seat.name}",
                       f"{seat.name}: account {seat.account!r} is not connected here")
            return
        platform, keeps_history = described
        chats = await self._chats(seat)
        spec = launch_spec(seat, platform, keeps_history, await self._name(seat, platform))
        want = spec_hash({**spec, "node": seat.node})
        rec = self.dao.keeper(seat.name)
        if rec and rec["node"] != seat.node:
            rec = None
        if rec and rec["spec_hash"] == legacy_hash({**spec, "node": seat.node}, seat):
            self.dao.set_keeper(seat.name, seat.node, rec["job"], want)
            rec = self.dao.keeper(seat.name)
        row = next((r for r in rows if rec and r.get("job") == rec["job"] and _live(r)), None)
        if row is None:
            adopted = self._adoptable(seat, rows, rec, spec["name"])
            if adopted is not None:
                self.dao.set_keeper(seat.name, seat.node, adopted["job"], want)
                log.info("keeper: adopted %s for %s; its start outlived the call",
                         adopted["job"], seat.name)
                rec, row = self.dao.keeper(seat.name), adopted
        st = self._state(seat)
        now = self._now()
        if row is not None:
            health = classify(row)
            if health.alive:
                self._report(seat, health)
                await self._alive(seat, st, rec, row, spec, want, chats, keeps_history)
                return
            self._report(seat, health)
            if health.state == BLOCKED:
                return
            situation = Situation(FAILED, daemon_up=_daemon_up(daemon))
            self._open(seat, st, rec, daemon, f"it failed ({health.reason})")
        else:
            situation = Situation("exited" if rec else "missing",
                                  daemon_up=_daemon_up(daemon),
                                  resumable=bool(rec) and st.unresumable != rec["job"],
                                  spec_changed=bool(rec) and rec["spec_hash"] != want)
            self._report(seat, Health(situation.state, "", rec["job"] if rec else None))
            if rec:
                self._open(seat, st, rec, daemon, "its session ended")
        st.alive_since = None
        action, why = decide(situation, st.breaker, now)
        if action is Action.RESUME and not self._name_free(seat, rows, spec["name"], rec["job"]):
            return
        if action is Action.RESUME:
            if await self._resume(seat, st, rec, chats, keeps_history):
                return
            action, why = decide(Situation(situation.state, daemon_up=situation.daemon_up,
                                           resumable=False), st.breaker, now)
        if action is Action.FRESH:
            if not self._name_free(seat, rows, spec["name"], rec["job"] if rec else None):
                return
            await self._fresh(seat, st, spec, want, rec, chats, keeps_history, why,
                              counted=rec is not None)
            return
        if action is Action.GIVE_UP:
            self._report(seat, Health(BROKEN, why, rec["job"] if rec else None))
            if not st.broken_alerted:
                st.broken_alerted = True
                self._broke.append(seat)
                log.warning("keeper: %s is broken: %s; not trying again until "
                            "keep_reset or the breaker clears", seat.name, why)
        elif action is Action.WAIT:
            self._report(seat, Health(WAITING, why, rec["job"] if rec else None))
        elif action is Action.BACKOFF:
            self._report(seat, Health(RESUMING, why, rec["job"] if rec else None))

    async def _alive(self, seat: SeatConfig, st: SeatState, rec: dict, row: dict,
                     spec: dict, want: str, chats: list, keeps_history: bool) -> None:
        now = self._now()
        if rec["spec_hash"] != want:
            if _mid_turn(row):
                self._note(f"outdated:{seat.name}", f"{seat.name}: {row['job']} "
                           "runs an outdated spec; replacing it after its turn")
            elif now >= st.breaker.next_try:
                await self._fresh(seat, st, spec, want, rec, chats, keeps_history,
                                  "its launch spec changed", counted=False)
                return
        await self._hold(seat, row["job"], chats)
        self.dao.keeper_seen(seat.name, now)
        st.unresumable = None
        if st.alive_since is None:
            st.alive_since = now
        if st.breaker.healthy(now, st.alive_since):
            st.broken_alerted = False
            log.info("keeper: %s settled after %s", seat.name, _span(now - st.alive_since))
        if st.outage is not None:
            self._close(seat, st, row["job"], chats, keeps_history)
        else:
            self._restart_catch_up(seat, row["job"], chats, keeps_history)

    def _name_free(self, seat: SeatConfig, rows: list[dict], name: str, job: str | None) -> bool:
        taken = [r for r in rows if r.get("name") == name and _live(r) and r.get("job") != job]
        if taken:
            self._note(f"name:{seat.name}", f"{seat.name}: session {taken[0].get('job')} "
                       f"already holds the name {name!r}; leaving the seat as it is")
        return not taken

    def _adoptable(self, seat: SeatConfig, rows: list[dict], rec: dict | None,
                   name: str) -> dict | None:
        return next((r for r in rows if _live(r) and r.get("job")
                     and r.get("name") == name and r.get("parent") is None
                     and (rec is None or r["job"] != rec["job"])), None)

    # -- outages ----------------------------------------------------------

    def _open(self, seat: SeatConfig, st: SeatState, rec: dict, daemon: dict | None,
              own_cause: str) -> None:
        """Open the seat's restart record, or reopen it after an attempt that did not hold."""
        if st.outage is not None:
            st.outage.up_at = st.outage.action = st.outage.missed = None
            return
        now = self._now()
        seen = _seen_epoch(rec)
        down_at = min(seen, now) if seen is not None else now
        cause = own_cause
        state, since = (daemon or {}).get("state"), _epoch((daemon or {}).get("since"))
        if state == "down":
            cause = (daemon or {}).get("cause") or "the Claude Code daemon on its node stopped"
            if since is not None and (seen is None or since >= seen):
                down_at = min(since, now)
        elif state == "up" and since is not None and seen is not None and since > seen:
            # The daemon restarted after the seat was last seen alive, and the
            # seat did not come back with it.
            cause = (daemon or {}).get("cause") or cause
        st.outage = Outage(down_at=down_at, cause=cause)
        log.warning("keeper: %s (%s) is down since %s: %s", seat.name, rec["job"],
                    _hhmm(down_at), st.outage.describe_cause())

    def _missed(self, seat: SeatConfig, chats: list, keeps_history: bool,
                since: float, until: float | None = None) -> int | None:
        if not keeps_history:
            return None
        ids = {c.id for c in chats}
        rows = self.dao.history(seat.account, since=f"{since:.3f}", limit=10_000)
        return sum(1 for r in rows if not r.get("own") and r.get("channel_id") in ids
                   and (until is None or float(r.get("ts") or 0) <= until))

    def _close(self, seat: SeatConfig, st: SeatState, job: str, chats: list,
               keeps_history: bool) -> None:
        outage = st.outage
        now = self._now()
        if outage.up_at is None:
            outage.up_at = now
            outage.missed = self._missed(seat, chats, keeps_history, outage.down_at)
            if self._push and (outage.missed or now - outage.down_at >= BLIP_S):
                self._push(seat.node, job, outage.render(
                    node=seat.node,
                    what_now=catch_up_line(seat, keeps_history, outage.down_at)))
        log.info("keeper: %s is back after %s", seat.name, _span(now - outage.down_at))
        if outage.alerted_at is not None:
            self._recovered.append((seat, outage))
        st.last_outage = outage.to_dict()
        st.outage = None

    def _restart_catch_up(self, seat: SeatConfig, job: str, chats: list,
                          keeps_history: bool) -> None:
        """Once per process: tell a seat what arrived while social itself was down."""
        if self._booted_at is None or self._now() - self._booted_at < self._interval_s:
            return
        seen = self._boot_seen.pop(seat.name, None)
        if seen is None or self._push is None:
            return
        missed = self._missed(seat, chats, keeps_history, seen, until=self._booted_at)
        if not missed:
            return
        self._push(seat.node, job, (
            f"social keeper: social restarted, and {missed} message"
            f"{'s' if missed != 1 else ''} that arrived from {_hhmm(seen)} to "
            f"{_hhmm(self._booted_at)} may not have been pushed to you. "
            + catch_up_line(seat, keeps_history, seen)
            + " Do not announce the restart in the chat."))

    # -- attempts ---------------------------------------------------------

    async def _resume(self, seat: SeatConfig, st: SeatState, rec: dict, chats: list,
                      keeps_history: bool) -> bool:
        """Bring the recorded job back. False means start a new agent instead."""
        now, job = self._now(), rec["job"]
        reply = await self.cx.resume(seat.node, job)
        if reply.get("code") == DAEMON_DOWN:
            self._report(seat, Health(WAITING, "no Claude Code daemon runs on its node", job))
            return True
        if not reply.get("ok"):
            self._note(f"resume:{seat.name}", f"{seat.name}: cx would not resume {job}: "
                       f"{reply.get('reason') or reply!r}; starting a new agent")
            st.unresumable = job
            return False
        st.breaker.attempt(now, "resume")
        st.alive_since = now
        await self._hold(seat, job, chats)
        self._report(seat, Health("running", "", job))
        outage = st.outage or Outage(down_at=_seen_epoch(rec) or now, cause="its session ended")
        outage.up_at, outage.action = now, "resumed"
        outage.missed = self._missed(seat, chats, keeps_history, outage.down_at)
        st.outage = outage
        if self._push:
            self._push(seat.node, job, outage.render(
                node=seat.node, what_now=catch_up_line(seat, keeps_history, outage.down_at)))
        log.info("keeper: resumed %s for %s on %s", job, seat.name, seat.node)
        return True

    async def _fresh(self, seat: SeatConfig, st: SeatState, spec: dict, want: str,
                     rec: dict | None, chats: list, keeps_history: bool, why: str,
                     counted: bool = True) -> None:
        now = self._now()
        old = rec["job"] if rec else None
        args = dict(spec)
        outage = st.outage
        if outage is not None:
            outage.up_at, outage.action = now, "fresh"
            outage.missed = self._missed(seat, chats, keeps_history, outage.down_at)
            args["prompt"] = "\n\n".join([outage.render(
                node=seat.node, what_now=catch_up_line(seat, keeps_history, outage.down_at)),
                spec["prompt"]])
        if old:
            args["replaces"] = old
        reply = await self.cx.start(seat.node, args)
        if reply.get("code") == DAEMON_DOWN:
            if outage is not None:
                outage.up_at = outage.action = outage.missed = None
            self._report(seat, Health(WAITING, "no Claude Code daemon runs on its node", old))
            return
        if not (reply.get("ok") and reply.get("job")):
            if outage is not None:
                outage.up_at = outage.action = outage.missed = None
            reason = reply.get("reason") or reply.get("error") or repr(reply)
            st.breaker.attempt(now, "fresh", f"cx would not start it: {_short(reason)}")
            self._report(seat, Health(RESUMING, f"cx would not start it: {_short(reason)}", old))
            self._note(f"start:{seat.name}", f"{seat.name}: cx would not start it: {reason}")
            return
        if counted:
            st.breaker.attempt(now, "fresh")
        job = reply["job"]
        st.alive_since, st.unresumable = now, None
        self.dao.set_keeper(seat.name, seat.node, job, want)
        await self._hold(seat, job, chats)
        self._report(seat, Health("running", "", job))
        log.info("keeper: started %s for %s on %s (%s)", job, seat.name, seat.node, why)

    async def _hold(self, seat: SeatConfig, job: str, chats: list) -> None:
        """Make ``job`` the holder of each of the seat's chats, unless it already is."""
        for chat in chats:
            held = self.dao.holder(seat.account, chat.id)
            if held and (held["node"], held["job"]) == (seat.node, job):
                continue
            name = (chat.name or self._chat_names.get(seat.name)
                    or (held or {}).get("channel_name") or "")
            self.dao.mount(seat.account, chat.id, name,
                           {"node": seat.node, "job": job, "name": self._names.get(seat.name, "")})

    # -- alerts -----------------------------------------------------------

    async def _send_alerts(self) -> None:
        """One message to the operator about every seat that went down, came back or broke."""
        now = self._now()
        down = []
        for seat in self.seats:
            st = self._states.get(seat.name)
            outage = st.outage if st else None
            if (outage is None or outage.alerted_at is not None or outage.up_at is not None
                    or now - outage.down_at < seat.alert_after):
                continue
            if st.last_alert_at is not None and now - st.last_alert_at < ALERT_EVERY_S:
                continue
            down.append((seat, st, outage))
        lines = []
        for seat, _, outage in down:
            doing = self.health.get(seat.name)
            lines.append(f"- {seat.name} on {seat.node} is down since {_hhmm(outage.down_at)} "
                         f"({_span(now - outage.down_at)}) because {outage.describe_cause()}."
                         + (f" The keeper is {doing.state}: {doing.reason}." if doing and
                            doing.reason else ""))
        for seat in self._broke:
            st = self._states.get(seat.name)
            lines.append(f"- {seat.name} on {seat.node} is broken: {st.breaker.reason}. "
                         "The keeper stopped trying. Clear it with "
                         f"`social keep_reset seat={seat.name}`.")
        for seat, outage in self._recovered:
            how = {"resumed": "resumed with its own conversation",
                   "fresh": "as a new session"}.get(outage.action or "", "by itself")
            lines.append(f"- {seat.name} on {seat.node} is back after "
                         f"{_span((outage.up_at or now) - outage.down_at)}, {how}.")
        if not lines:
            return
        text = "social keeper (an automatic notice, not a message from Tony):\n" + "\n".join(lines)
        if self._alert is None:
            log.warning("keeper: no alert route; %s", text)
        else:
            try:
                await self._alert(text)
            except Exception as exc:  # noqa: BLE001 — retried next tick
                log.warning("keeper: could not send the alert: %s", exc)
                return
        for seat, st, outage in down:
            outage.alerted_at = st.last_alert_at = now
            self._save(seat)
        self._broke.clear()
        self._recovered.clear()

    # -- notes ------------------------------------------------------------

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
