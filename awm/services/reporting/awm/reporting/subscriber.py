"""The board subscriber: cards addressed to this swarm go into the queue.

Two paths feed the queue and both are idempotent by card id. The stream is
live and resumes from a saved cursor. The catch-up reads the board directly,
which closes the gaps the stream cannot: a cursor the board no longer holds
(``resync``), a first start with no cursor, and a long silence.

A request card is claimed before it is queued, so two doors never queue the
same card. The door writes a ``claiming`` row first. A crash between the claim
and the queue write then leaves a row only this door could have made, and the
recovery path adopts exactly those cards: a card the board shows as held by
this swarm with no such row was claimed by someone else in the swarm and is
left alone. A claim that answers 409 means someone else holds it, and the door
skips the card. A message card is queued and never claimed, because the board
refuses to claim one.

The stream cursor is saved only after the event's card write has committed.
A failure part way through leaves the cursor behind, and the restart replays
the event. A catch-up that fails never keeps the stream from starting.

A message on a card's thread wakes the session the card was assigned to, with
a line that names the card and carries none of its text. A session that cannot
take the line gets one more try, from the catch-up the miss asks for. After
that, or when the card has no session, the card goes back to the
representative's queue. Each card's thread cursor moves only once its message
reached someone, so a catch-up after downtime routes what the stream missed.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from datetime import datetime, timezone
from typing import Any, Callable

import httpx

from awm.board.client import RESYNC, BoardConflict, BoardError, BoardRefused

from awm.reporting import CLAIMING, DONE, FAILED, GONE, TERMINAL
from awm.reporting.store import Queue

log = logging.getLogger("awm.reporting.subscriber")

POSTED = "card.posted"
MOVED = "card.moved"
MESSAGE = "card.message"
PAGE = 200
MAX_PAGES = 50
RESTART_DELAY_S = 2.0
KICK_DELAY_S = 10.0
MAX_RESTART_DELAY_S = 120.0
REFUSED_DELAY_S = 300.0


def thread_text(card_id: str) -> str:
    return f"New message on board card {card_id}, run operations board_messages"


def _send_line(job: str, text: str) -> None:
    from awm.claudedaemon.job import send_line

    send_line(job, text)


def _swarm_of(sender: Any) -> str:
    return str(sender.get("swarm") or "") if isinstance(sender, dict) else str(sender or "")


class Subscriber:
    """Reads the board for one swarm and writes the queue."""

    def __init__(self, queue: Queue, client: Any, swarm: str, *,
                 on_queued: Callable[[], None] | None = None,
                 catchup_s: float = 300.0,
                 message_backlog_s: float = 3 * 86400.0,
                 send: Callable[[str, str], None] = _send_line,
                 clock: Callable[[], float] = time.time) -> None:
        self.queue = queue
        self.client = client
        self.swarm = swarm
        self._on_queued = on_queued or (lambda: None)
        self._catchup_s = catchup_s
        self._backlog_s = message_backlog_s
        self._send = send
        self._clock = clock
        self._missed: set[str] = set()
        self._lock = asyncio.Lock()
        self._progress = False
        self._kick = asyncio.Event()
        self.attached = False
        self.last_error: str | None = None

    # -- one card ------------------------------------------------------------

    def _queued(self, card: dict, *, reopen: bool = False) -> None:
        if self.queue.enqueue(card, reopen=reopen):
            log.info("reporting: queued %r card %s (%r)", card.get("kind"), card["id"],
                     card.get("priority"))
            self._on_queued()

    async def _intake(self, card: dict, *, claim_known: bool, reopen: bool = False) -> bool:
        """Queue a card addressed to us. Returns True if the card is now queued by us.

        ``claim_known`` claims a request even when the queue already holds it:
        the caller has current board state saying the card is posted again.
        """
        if card.get("kind") == "message":
            self._queued(card, reopen=reopen)
            return True
        held = self.queue.status_of(card["id"])
        if held is not None and held != CLAIMING and not claim_known:
            return False  # a `claiming` row is retried: a claim by the same swarm is idempotent
        marked = self.queue.begin_claim(card)
        try:
            claimed = await self.client.claim(card["id"])
        except BoardConflict:
            log.info("reporting: card %s is held by someone else; skipped", card["id"])
            self._unmark(card, marked)
            return False
        except BoardRefused:
            log.info("reporting: the board will not let this swarm claim %s; skipped", card["id"])
            self._unmark(card, marked)
            return False
        merged = {**card, **(claimed if isinstance(claimed, dict) else {})}
        self._queued(merged, reopen=True if claim_known else reopen)
        return True

    def _unmark(self, card: dict, marked: bool) -> None:
        if marked:
            self.queue.drop_claim(card["id"])

    def _adopt(self, card: dict, *, authoritative: bool) -> None:
        """Queue a card the board shows as held by this swarm, if the door claimed it.

        The door's own mark is the evidence: a ``claiming`` row, or a finished
        row that the board now shows in progress under our name (a reopened card
        whose claim landed before the crash). ``authoritative`` is the current
        board state; a replayed event may be older than the queue, so it adopts
        only on the ``claiming`` mark.
        """
        status = self.queue.status_of(card["id"])
        if status == CLAIMING or (authoritative and status in TERMINAL):
            log.info("reporting: adopting card %s, claimed before the queue write landed", card["id"])
            self._queued(card, reopen=True)

    # -- one thread message --------------------------------------------------

    def _ours(self, card: dict) -> bool:
        return card.get("recipient") == self.swarm or _swarm_of(card.get("sender")) == self.swarm

    async def _on_message(self, card: dict) -> None:
        message = card.get("message")
        if not isinstance(message, dict) or not self._ours(card):
            return
        seq = int(message.get("seq") or 0)
        if seq <= self.queue.thread_seq(card["id"]):
            return
        if _swarm_of(message.get("sender")) == self.swarm or await self._route(card):
            self.queue.set_thread_seq(card["id"], seq)

    async def _route(self, card: dict) -> bool:
        """Tell the card's session, or the representative, about a new message. False to retry."""
        held = self.queue.get(card["id"])
        if held is not None and held["status"] == CLAIMING:
            return True  # the card reaches the representative with its thread
        job = held.get("assigned_job") if held else None
        if job:
            try:
                await asyncio.to_thread(self._send, job, thread_text(card["id"]))
            except Exception as exc:  # noqa: BLE001 — a closed PTY or a dialog is a miss
                if card["id"] not in self._missed:
                    self._missed.add(card["id"])
                    log.info("reporting: %s did not take card %s's message (%s); retrying",
                             job, card["id"], exc)
                    self._kick.set()
                    return False
                log.info("reporting: %s is gone; card %s goes back to the representative",
                         job, card["id"])
            else:
                self._missed.discard(card["id"])
                log.info("reporting: told %s about a message on card %s", job, card["id"])
                return True
        self._missed.discard(card["id"])
        self.queue.reopen_thread(card)
        log.info("reporting: queued card %s for a message on its thread", card["id"])
        self._on_queued()
        return True

    async def _apply(self, type_: str, card: dict) -> None:
        if type_ == MESSAGE:
            await self._on_message(card)
            return
        if card.get("recipient") != self.swarm:
            if self.queue.has(card["id"]) and self.queue.mark(card["id"], GONE):
                log.info("reporting: card %s was re-addressed away from this swarm", card["id"])
            return
        status = card.get("status")
        if status == "posted" and type_ in (POSTED, MOVED):
            await self._intake(card, claim_known=type_ == MOVED, reopen=type_ == MOVED)
        elif status in (DONE, FAILED):
            if self.queue.mark(card["id"], status):
                log.info("reporting: card %s is %s on the board", card["id"], status)
        elif (status == "in_progress" and card.get("kind") == "request"
              and card.get("claimant") == self.swarm):
            self._adopt(card, authoritative=False)

    async def _handle(self, event_id: int, type_: str, card: dict) -> None:
        if type_ == RESYNC:
            await self._catch_up()
            self.queue.set_cursor(event_id, force=True)
        else:
            try:
                await self._apply(type_, card)
            except (BoardError, httpx.HTTPError) as exc:
                # Retrying the same event would fail the same way for ever. The
                # card keeps its `claiming` mark and the next catch-up takes it.
                log.warning("reporting: event %s on card %s failed (%s); left for the catch-up",
                            event_id, card.get("id"), exc)
                self.last_error = f"event {event_id} failed: {exc}"
                self._kick.set()
            self.queue.set_cursor(event_id)
        self._progress = True

    # -- catch-up ------------------------------------------------------------

    async def catch_up(self) -> int:
        """Bring the queue in line with the board. Returns the number of cards that failed."""
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
        for card in await self._all(recipient=self.swarm, kind="request", status="posted"):
            try:
                await self._intake(card, claim_known=True)
            except BoardError as exc:
                failed += 1
                log.warning("reporting: could not take card %s in catch-up: %s", card["id"], exc)
        for card in await self._all(recipient=self.swarm, kind="request",
                                    status="in_progress", claimant=self.swarm):
            self._adopt(card, authoritative=True)
        for card in await self._all(recipient=self.swarm, kind="message", status="posted"):
            if not self.queue.has(card["id"]) and self._recent(card):
                self._queued(card)
        for card_id in self.queue.active_ids():
            try:
                current = await self.client.get(card_id)
            except BoardRefused:
                if self.queue.mark(card_id, GONE):
                    log.info("reporting: card %s is no longer on the board", card_id)
                continue
            except BoardError as exc:
                failed += 1
                log.warning("reporting: could not re-read card %s: %s", card_id, exc)
                continue
            if current.get("recipient") != self.swarm:
                if self.queue.mark(card_id, GONE):
                    log.info("reporting: card %s was re-addressed away from this swarm", card_id)
            elif current.get("status") in (DONE, FAILED):
                self.queue.mark(card_id, current["status"])
        return failed + await self._catch_up_threads()

    async def _catch_up_threads(self) -> int:
        """Route thread messages the stream missed. Returns the number of cards that failed."""
        seen = self.queue.thread_seqs()
        cards = {c["id"]: c for c in (await self._all(recipient=self.swarm)
                                      + await self._all(sender=self.swarm))}
        failed = 0
        for card in cards.values():
            after = seen.get(card["id"], 0)
            if int(card.get("last_message") or 0) <= after:
                continue
            try:
                fresh = await self.client.messages(card["id"], after=after)
            except BoardRefused:
                continue
            except BoardError as exc:
                failed += 1
                log.warning("reporting: could not read card %s's thread: %s", card["id"], exc)
                continue
            if not fresh:
                continue
            theirs = any(_swarm_of(m.get("sender")) != self.swarm for m in fresh)
            if not theirs or await self._route(card):
                self.queue.set_thread_seq(card["id"], max(int(m["seq"]) for m in fresh))
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
        stream = self.client.stream(self.queue.cursor(), on_state=self._set_attached)
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
        periodic = asyncio.create_task(self._periodic(), name="reporting-catchup")
        try:
            await self._stream_loop()
        finally:
            periodic.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await periodic
