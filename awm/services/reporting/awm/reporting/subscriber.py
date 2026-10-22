"""The board subscriber: cards addressed to this swarm become Proposed, and Tony is told once.

Two paths feed the queue and both are idempotent by card id. The stream is
live and resumes from a saved cursor. The catch-up reads the board directly,
which closes the gaps the stream cannot: a cursor the board no longer holds
(``resync``), a first start with no cursor, and a long silence.

The door never claims a card and never shows a card's text to an agent. A card
addressed to this swarm or to ``open``, or posted by this swarm, is queued as
``proposed`` and Tony gets one direct message with the claim reference, unless
the other party is a swarm in ``AWM_REPORTING_QUIET_SWARMS``. A
message that fails to send leaves the card unannounced, and the next catch-up
sends it. An agent takes a card with ``task_claim``, which assigns the card to
the agent's session.

A message on a card's thread wakes the session the card was assigned to, with
a line that names the card and sender and carries none of its text. A session
that cannot take the line gets one more try, from the catch-up the miss asks
for. After that, or when the card has no session, it waits on the thread.
Each card's thread cursor moves only once its message reached someone, so a
catch-up after downtime routes what the stream missed.

The jobs that watch a card, every card an agent posted, a project or another
swarm are typed one line per event. A node that is not the door runs the
subscriber with ``intake=False``: it only types those lines, so a session on
any node with a board token hears its cards' events.

The stream cursor is saved only after the event's card write has committed.
A failure part way through leaves the cursor behind, and the restart replays
the event. A catch-up that fails never keeps the stream from starting.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

import httpx

from awm.board.client import RESYNC, BoardError, BoardRefused

from awm.reporting import ACTIVE, ASSIGNED, DONE, GONE, PROPOSED, REJECTED, config
from awm.reporting.intake import (card_dm, card_state, event_line, send_dm, swarm_of,
                                  wake_text)
from awm.reporting.store import Offloaded, Queue

log = logging.getLogger("awm.reporting.subscriber")

POSTED = "card.posted"
CLAIMED = "card.claimed"
MOVED = "card.moved"
COMPLETED = "card.completed"
FAILED = "card.failed"
MESSAGE = "card.message"
OPEN = "open"
PAGE = 200
MAX_PAGES = 50
RESTART_DELAY_S = 2.0
KICK_DELAY_S = 10.0
MAX_RESTART_DELAY_S = 120.0
#: The edge answers a board that is down with the same 404 as a revoked token, so a refusal
#: is retried soon: a host restart must not leave the door deaf for minutes.
REFUSED_DELAY_S = 30.0
_FINISHED = {"done": DONE, "rejected": REJECTED}


def _send_line(job: str, text: str) -> None:
    from awm.claudedaemon.job import send_line

    send_line(job, text)


class Subscriber:
    """Reads the board for one swarm and writes the queue."""

    def __init__(self, queue: Queue, client: Any, swarm: str, *,
                 dm: Callable[[str], Awaitable[None]] = send_dm,
                 on_closed: Callable[[str, str], Awaitable[None]] | None = None,
                 catchup_s: float = 300.0,
                 message_backlog_s: float = 3 * 86400.0,
                 send: Callable[[str, str], None] = _send_line,
                 clock: Callable[[], float] = time.time,
                 intake: bool = True) -> None:
        self.queue = queue
        self.db = Offloaded(queue)
        self.client = client
        self.swarm = swarm
        self._dm = dm
        self._on_closed = on_closed
        self._catchup_s = catchup_s
        self._backlog_s = message_backlog_s
        self._send = send
        self._clock = clock
        self.intake = intake
        self._missed: set[str] = set()
        self._lock = asyncio.Lock()
        self._progress = False
        self._kick = asyncio.Event()
        self.attached = False
        self.last_error: str | None = None

    # -- which cards are ours --------------------------------------------------

    def _mine(self, card: dict) -> bool:
        """A card to this swarm or to anyone, or one this swarm posted."""
        return (card.get("recipient") in (self.swarm, OPEN)
                or swarm_of(card.get("sender")) == self.swarm)

    def _claimable(self, card: dict) -> bool:
        return card.get("recipient") in (self.swarm, OPEN)

    @staticmethod
    def _quiet(card: dict) -> bool:
        """A card to or from a swarm in ``AWM_REPORTING_QUIET_SWARMS``: queued, never DMed."""
        quiet = config.quiet_swarms()
        return bool(quiet) and (card.get("recipient") in quiet
                                or swarm_of(card.get("sender")) in quiet)

    # -- one card ------------------------------------------------------------

    async def _intake(self, card: dict, *, reopen: bool = False, quiet: bool = False) -> None:
        """Queue a card as proposed, and tell Tony if he has not been told."""
        if await self.db.enqueue(card, quiet=quiet, reopen=reopen):
            log.info("reporting: queued %r card %s", card.get("kind"), card["id"])
        if await self.db.needs_dm(card["id"]):
            if self._quiet(card):
                await self.db.mark_notified(card["id"])
                return
            try:
                await self._dm(card_dm(card, claimable=self._claimable(card)))
            except Exception as exc:  # noqa: BLE001 — the catch-up sends it again
                log.warning("reporting: could not tell Tony about card %s: %s", card["id"], exc)
                self.last_error = f"direct message failed: {exc}"
                self._kick.set()
                return
            await self.db.mark_notified(card["id"])

    async def _left(self, card: dict) -> None:
        """A card this swarm does not hold any more: another swarm took it, or it was re-addressed."""
        if swarm_of(card.get("sender")) != self.swarm and await self.db.mark(card["id"], GONE):
            log.info("reporting: card %s left this swarm's hands", card["id"])

    async def _finished(self, card: dict, state: str) -> None:
        job = await self._assignee(card["id"]) if state == "rejected" else None
        if await self.db.mark(card["id"], _FINISHED[state]):
            log.info("reporting: card %s is %s on the board", card["id"], state)
            if job:
                await self._tell(job, event_line(FAILED, card))
        if self._on_closed is not None:
            await self._on_closed(card["id"], state)

    # -- one thread message --------------------------------------------------

    async def _on_message(self, card: dict) -> None:
        message = card.get("message")
        if not isinstance(message, dict) or not self._mine(card):
            return
        seq = int(message.get("seq") or 0)
        if seq <= await self.db.thread_seq(card["id"]):
            return
        if not self.intake:
            await self.db.set_thread_seq(card["id"], seq)
            await self._tell_watchers(MESSAGE, card)
            return
        if swarm_of(message.get("sender")) == self.swarm or await self._route(card, message):
            await self.db.set_thread_seq(card["id"], seq)
            await self._tell_watchers(MESSAGE, card, skip=await self._assignee(card["id"]))

    async def _assignee(self, card_id: str) -> str | None:
        held = await self.db.get(card_id)
        return held.get("assigned_job") if held and held["status"] == ASSIGNED else None

    async def _route(self, card: dict, message: dict) -> bool:
        """Wake the card's session about a new message. False to retry. Tony gets no DM."""
        job = await self._assignee(card["id"])
        if not job:
            return True
        try:
            await asyncio.to_thread(self._send, job, wake_text(card["id"], message))
        except Exception as exc:  # noqa: BLE001 — a closed PTY or a dialog is a miss
            if card["id"] not in self._missed:
                self._missed.add(card["id"])
                log.info("reporting: %s did not take card %s's message (%s); retrying",
                         job, card["id"], exc)
                self._kick.set()
                return False
            log.info("reporting: %s is gone; card %s's message stays on its thread", job, card["id"])
        else:
            log.info("reporting: told %s about a message on card %s", job, card["id"])
        self._missed.discard(card["id"])
        return True

    # -- watchers --------------------------------------------------------------

    async def _tell(self, job: str, line: str | None) -> None:
        try:
            await asyncio.to_thread(self._send, job, line)
        except Exception as exc:  # noqa: BLE001 — a busy or closed session misses a line
            log.info("reporting: %s did not take a line about card %s: %s", job, line, exc)

    async def _tell_watchers(self, type_: str, card: dict, *, skip: str | None = None) -> None:
        """Type one line into each session that follows the card. Never raises."""
        line = event_line(type_, card)
        if line is None:
            return
        ours = swarm_of(card.get("sender")) == self.swarm
        # A poster is not told of its own post.
        poster = card.get("agent") if ours and type_ != POSTED else None
        other = card.get("recipient") if ours else swarm_of(card.get("sender"))
        # A project or swarm watch follows the other swarm, not this one's own moves.
        broad = self._actor(type_, card) != self.swarm
        for job in await self.db.watchers_of(card["id"], poster,
                                             project=card.get("project") if broad else None,
                                             swarm=other if broad and other != OPEN else None):
            if job != skip:
                await self._tell(job, line)

    @staticmethod
    def _actor(type_: str, card: dict) -> str:
        """The swarm that caused the event, as far as the event says."""
        if type_ == MESSAGE:
            return swarm_of((card.get("message") or {}).get("sender"))
        if type_ == POSTED:
            return swarm_of(card.get("sender"))
        if type_ in (CLAIMED, COMPLETED, FAILED):
            return str(card.get("claimant") or "")
        return ""

    # -- the events ------------------------------------------------------------

    async def _apply(self, type_: str, card: dict) -> None:
        if type_ == MESSAGE:
            await self._on_message(card)
            return
        if not self.intake:
            if self._mine(card) and not (type_ == POSTED and card.get("reply_to")):
                await self._tell_watchers(type_, card)
            return
        if not self._mine(card):
            if await self.db.has(card["id"]):
                await self._left(card)
            return
        if card.get("reply_to"):
            if type_ != POSTED:
                await self._tell_watchers(type_, card)
            return                      # its thread message on the root carries it
        await self._tell_watchers(type_, card)
        state = card_state(card)
        if state == "proposed" and type_ in (POSTED, MOVED):
            await self._intake(card, reopen=type_ == MOVED)
        elif state in _FINISHED:
            await self._finished(card, state)
        elif (type_ == CLAIMED and card.get("claimant") and card["claimant"] != self.swarm
              and card.get("recipient") == OPEN):
            await self._left(card)

    async def _handle(self, event_id: int, type_: str, card: dict) -> None:
        if type_ == RESYNC:
            await self._catch_up()
            await self.db.set_cursor(event_id, force=True)
        else:
            try:
                await self._apply(type_, card)
            except (BoardError, httpx.HTTPError) as exc:
                # Retrying the same event would fail the same way for ever.
                # The next catch-up reads the board and closes the gap.
                log.warning("reporting: event %s on card %s failed (%s); left for the catch-up",
                            event_id, card.get("id"), exc)
                self.last_error = f"event {event_id} failed: {exc}"
                self._kick.set()
            await self.db.set_cursor(event_id)
        self._progress = True

    # -- catch-up ------------------------------------------------------------

    async def catch_up(self) -> int:
        """Bring the queue in line with the board. Returns the number of cards that failed.

        A watch-only subscriber has no queue to mend: the stream's replay from
        its cursor is its catch-up.
        """
        if not self.intake:
            return 0
        async with self._lock:
            return await self._catch_up()

    async def _all(self, **filters: str) -> list[dict]:
        """Every card matching the filters, paged so no card past the first page is missed."""
        out: list[dict] = []
        for page in range(MAX_PAGES):
            cards = await self.client.list(limit=PAGE, offset=page * PAGE, **filters)
            out.extend(cards)
            if len(cards) < PAGE:
                return out
        log.warning("reporting: stopped reading %s after %d cards", filters, len(out))
        return out

    async def _catch_up(self) -> int:
        failed = 0
        seen: set[str] = set()
        for filters in ({"recipient": self.swarm}, {"recipient": OPEN}, {"sender": self.swarm}):
            for card in await self._all(state="proposed", **filters):
                if card["id"] in seen or card.get("reply_to"):
                    continue
                seen.add(card["id"])
                try:
                    await self._intake(card, quiet=not self._recent(card))
                except BoardError as exc:
                    failed += 1
                    log.warning("reporting: could not take card %s in catch-up: %s", card["id"], exc)
        for card_id in await self.db.active_ids():
            try:
                current = await self.client.get(card_id)
            except BoardRefused:
                if await self.db.mark(card_id, GONE):
                    log.info("reporting: card %s is no longer on the board", card_id)
                continue
            except BoardError as exc:
                failed += 1
                log.warning("reporting: could not re-read card %s: %s", card_id, exc)
                continue
            state = card_state(current)
            if not self._mine(current):
                await self._left(current)
            elif state in _FINISHED:
                await self._finished(current, state)
            elif (state != "proposed" and current.get("recipient") == OPEN
                  and current.get("claimant") != self.swarm):
                await self._left(current)
        return failed + await self._catch_up_threads()

    async def _catch_up_threads(self) -> int:
        """Route thread messages the stream missed. Returns the number of cards that failed."""
        seen = await self.db.thread_seqs()
        cards = {c["id"]: c for filters in ({"recipient": self.swarm}, {"recipient": OPEN},
                                            {"sender": self.swarm})
                 for c in await self._all(**filters)}
        failed = 0
        for card in cards.values():
            after = seen.get(card["id"])
            last = int(card.get("last_message") or 0)
            if last <= (after or 0):
                continue
            if after is None and (card_state(card) in _FINISHED or not self._recent(card)):
                await self.db.set_thread_seq(card["id"], last)  # history, not news
                continue
            try:
                fresh = await self.client.messages(card["id"], after=after or 0)
            except BoardRefused:
                continue
            except BoardError as exc:
                failed += 1
                log.warning("reporting: could not read card %s's thread: %s", card["id"], exc)
                continue
            if not fresh:
                continue
            theirs = [m for m in fresh if swarm_of(m.get("sender")) != self.swarm]
            if not theirs or await self._route(card, theirs[-1]):
                await self.db.set_thread_seq(card["id"], max(int(m["seq"]) for m in fresh))
        return failed

    def _recent(self, card: dict) -> bool:
        try:
            created = datetime.fromisoformat(str(card.get("created_at") or ""))
        except ValueError:
            return True
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        return self._clock() - created.timestamp() <= self._backlog_s

    # -- the stream ----------------------------------------------------------

    def _set_attached(self, state: bool) -> None:
        self.attached = state

    async def consume(self) -> None:
        """Read the stream from the saved cursor until it ends or an event fails."""
        stream = self.client.stream(await self.db.cursor(), on_state=self._set_attached)
        try:
            async for event_id, type_, card in stream:
                async with self._lock:
                    await self._handle(event_id, type_, card)
        finally:
            self.attached = False
            aclose = getattr(stream, "aclose", None)
            if aclose is not None:
                await aclose()

    async def _stream_loop(self) -> None:
        failures = 0
        while True:
            self._progress = False
            delay: float | None = None
            try:
                try:
                    failed = await self.catch_up()
                    if failed:
                        self.last_error = f"{failed} card(s) could not be taken in the last catch-up"
                except asyncio.CancelledError:
                    raise
                except BoardRefused:
                    raise
                except Exception as exc:  # noqa: BLE001 — the stream starts whatever the catch-up did
                    self.last_error = f"catch-up failed: {exc}"
                    log.warning("reporting: catch-up failed (%s); starting the stream anyway", exc)
                await self.consume()
                failures += 1
            except asyncio.CancelledError:
                raise
            except BoardRefused as exc:
                self.last_error = f"the board refused this swarm's token: {exc}"
                log.error("reporting: %s", self.last_error)
                delay = REFUSED_DELAY_S
            except (BoardError, OSError, asyncio.TimeoutError) as exc:
                self.last_error = str(exc)
                log.warning("reporting: board subscription failed (%s); restarting", exc)
                failures += 1
            except Exception as exc:  # noqa: BLE001 — the door must outlive a bad card
                self.last_error = repr(exc)
                log.exception("reporting: subscriber failed; restarting")
                failures += 1
            if self._progress:
                failures = 0
                self.last_error = None
            if delay is None:  # a restartable failure backs off 2, 4, 8 ... seconds
                delay = min(MAX_RESTART_DELAY_S, RESTART_DELAY_S * 2 ** max(0, failures - 1))
            await asyncio.sleep(delay)

    async def _periodic(self) -> None:
        while True:
            try:
                await asyncio.wait_for(self._kick.wait(), timeout=self._catchup_s)
                await asyncio.sleep(KICK_DELAY_S)  # a failed event asks for a catch-up, not a spin
            except (TimeoutError, asyncio.TimeoutError):
                pass
            self._kick.clear()
            try:
                await self.catch_up()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.warning("reporting: periodic catch-up failed: %s", exc)

    async def run(self) -> None:
        """Subscribe forever. Never returns, never raises."""
        if not self.intake:
            await self._stream_loop()
            return
        periodic = asyncio.create_task(self._periodic(), name="reporting-catchup")
        try:
            await self._stream_loop()
        finally:
            periodic.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await periodic
