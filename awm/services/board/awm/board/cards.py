"""Cards: the board's rules, over notes in the Trilium vault.

The vault is the store. A card is a note under the board note, and its status is
the board-view column the note sits in, so a drag in the Trilium GUI is a real
status change. This process is the only API writer; the watcher (`Board.sweep`)
notices the GUI's writes and turns them into `card.moved` events. The event log
only feeds stream replay.

Claim safety rests on one service-wide lock: a threading lock for threads, and
an flock on `lock_path` held for the process lifetime so a second board process
refuses to start. A claim re-reads the card note under that lock and writes only
if the card is still posted and unclaimed. The GUI does not take the lock, so a
drag landing between that read and the write is overwritten (last write wins),
and a drag landing before the read makes the claim a Conflict.

Refusals use three exceptions. The door answers 404 for `NotFound` and
`Forbidden` and 409 for `Conflict`. A message card is never claimable: claiming
one raises `Conflict`, after the visibility and recipient checks.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import re
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .events import Events
from .parties import OPEN, Conflict, Forbidden, NotFound, can_see
from .vault import Vault, VaultError

__all__ = ["Board", "BoardLocked", "Conflict", "Forbidden", "NotFound"]

log = logging.getLogger("awm.board")

KINDS = ("request", "message")
PRIORITIES = ("urgent", "normal", "low")
STATUSES = ("posted", "in_progress", "done", "failed")

#: Status to the column name the board's `label:status` definition offers.
#: These four are the only `#status` values the board ever writes.
COLUMNS = {"posted": "Posted", "in_progress": "In progress",
           "done": "Done", "failed": "Failed"}
_STATUS_OF = {column: status for status, column in COLUMNS.items()}

RESULT_MARKER = "\n\n## Result\n\n"
_MAX_TEXT = 200_000

_SLUG = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


class BoardLocked(RuntimeError):
    """Another board holds the lock file, so a second writer would race it."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _split_content(content: str | None) -> tuple[str | None, str | None]:
    if content is None:
        return None, None
    body, marker, result = content.partition(RESULT_MARKER)
    return body, (result if marker else None)


class Board:
    """The card rules. One instance per process, enforced by the lock file."""

    def __init__(self, vault: Vault, events: Events, *, lock_path: str | Path):
        self._lock_fd = self._take_lock(Path(lock_path))
        self.vault = vault
        self.events = events
        self._guard = threading.RLock()
        self._seen: dict[str, str] = events.seen_all()

    # -- the process lock ------------------------------------------------------

    @staticmethod
    def _take_lock(path: Path) -> int:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            os.close(fd)
            raise BoardLocked(f"another board holds {path}") from e
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
        return fd

    def close(self) -> None:
        """Release the lock file. Dropping the process does the same."""
        fd, self._lock_fd = self._lock_fd, -1
        if fd >= 0:
            os.close(fd)

    def __enter__(self) -> "Board":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- notes to cards --------------------------------------------------------

    def _card(self, rec: dict) -> dict:
        labels = rec["labels"]
        body, result = _split_content(rec["content"])
        reply_to = None
        target = rec["relations"].get("replyTo")
        if target:
            try:
                reply_to = self.vault.card_id_for_note(target)
            except VaultError:
                reply_to = None
        column = labels.get("status") or ""
        return {
            "id": labels["cardId"],
            "kind": labels.get("cardKind") or "request",
            "sender": {"swarm": labels.get("cardFrom") or "",
                       "principal": labels.get("cardPrincipal") or "",
                       "party": labels.get("cardParty") or ""},
            "recipient": labels.get("cardTo") or "",
            "claimant": labels.get("cardClaimant") or None,
            "priority": labels.get("cardPriority") or "normal",
            "status": _STATUS_OF.get(column, column.lower() or "posted"),
            "title": rec["title"],
            "body": body,
            "reply_to": reply_to,
            "result": result,
            "created_at": labels.get("cardCreated") or "",
            "updated_at": labels.get("cardUpdated") or "",
        }

    def _remember(self, card_id: str, status: str) -> None:
        self._seen[card_id] = status
        self.events.seen_set(card_id, status)

    def _live(self, party: dict) -> dict:
        if not isinstance(party, dict) or not party.get("swarm") or party.get("revoked"):
            raise Forbidden("no live party")
        return party

    def _fetch(self, party: dict, card_id: str) -> tuple[dict, dict]:
        """The card's fresh record and card dict, or NotFound if the party may not see it."""
        rec = self.vault.read(card_id)
        if rec is None:
            raise NotFound(card_id)
        card = self._card(rec)
        if not can_see(party, card):
            raise NotFound(card_id)
        return rec, card

    # -- verbs -----------------------------------------------------------------

    def post(self, party: dict, *, kind: str, recipient: str, title: str, body: str,
             priority: str = "normal", reply_to: str | None = None) -> dict:
        self._live(party)
        if kind not in KINDS:
            raise ValueError(f"kind must be one of {KINDS}: {kind!r}")
        if priority not in PRIORITIES:
            raise ValueError(f"priority must be one of {PRIORITIES}: {priority!r}")
        if recipient != OPEN and not _SLUG.match(recipient or ""):
            raise ValueError(f"recipient must be a swarm slug or {OPEN!r}: {recipient!r}")
        title = (title or "").strip()
        if not title or len(title) > 300:
            raise ValueError("title must hold 1 to 300 characters")
        if not isinstance(body, str) or len(body) > _MAX_TEXT:
            raise ValueError("body must be text of at most %d characters" % _MAX_TEXT)
        if "## Result" in body:
            raise ValueError("body may not hold a '## Result' heading: the board uses it "
                             "to tell the body from the result")
        with self._guard:
            relations = {}
            if reply_to:
                parent, _ = self._fetch(party, reply_to)
                relations["replyTo"] = parent["note_id"]
            now = _now()
            labels = {
                "cardId": uuid.uuid4().hex,
                "cardKind": kind,
                "cardFrom": party["swarm"],
                "cardPrincipal": party["principal"],
                "cardParty": party["party_id"],
                "cardTo": recipient,
                "cardClaimant": "",
                "cardPriority": priority,
                "cardCreated": now,
                "cardUpdated": now,
                "status": COLUMNS["posted"],
            }
            rec = self.vault.create(title, body, labels, relations)
            card = self._card(rec)
            self.events.append("card.posted", card)
            self._remember(card["id"], card["status"])
            return card

    def claim(self, party: dict, card_id: str) -> dict:
        self._live(party)
        with self._guard:
            rec, card = self._fetch(party, card_id)
            if card["recipient"] not in (party["swarm"], OPEN):
                raise Forbidden("the card is not addressed to this swarm")
            if card["kind"] == "message":
                raise Conflict("a message cannot be claimed")
            if card["status"] != "posted" or card["claimant"]:
                raise Conflict("the card is already held")
            now = _now()
            self.vault.update(rec["note_id"], labels={
                "cardClaimant": party["swarm"], "cardUpdated": now,
                "status": COLUMNS["in_progress"]})
            card.update(claimant=party["swarm"], status="in_progress", updated_at=now)
            self.events.append("card.claimed", card)
            self._remember(card["id"], "in_progress")
            return card

    def complete(self, party: dict, card_id: str, result) -> dict:
        return self._finish(party, card_id, result, "done", "card.completed")

    def fail(self, party: dict, card_id: str, reason) -> dict:
        return self._finish(party, card_id, reason, "failed", "card.failed")

    def _finish(self, party: dict, card_id: str, result, status: str, event: str) -> dict:
        self._live(party)
        text = result if isinstance(result, str) else json.dumps(result)
        if len(text) > _MAX_TEXT:
            raise ValueError("result is too long")
        with self._guard:
            rec, card = self._fetch(party, card_id)
            if card["claimant"] != party["swarm"]:
                raise Forbidden("only the claimant finishes a card")
            if card["status"] != "in_progress":
                raise Conflict("the card is not in progress")
            now = _now()
            content = (card["body"] or "") + RESULT_MARKER + text
            self.vault.update(rec["note_id"], content=content, labels={
                "cardUpdated": now, "status": COLUMNS[status]})
            card.update(status=status, result=text, updated_at=now)
            self.events.append(event, card)
            self._remember(card["id"], status)
            return card

    def get(self, party: dict, card_id: str) -> dict:
        self._live(party)
        _, card = self._fetch(party, card_id)
        return card

    _FILTERS = ("sender", "recipient", "claimant", "status", "kind", "reply_to")

    def list(self, party: dict, **filters) -> list[dict]:
        self._live(party)
        unknown = set(filters) - set(self._FILTERS)
        if unknown:
            raise ValueError(f"unknown filter {sorted(unknown)}")
        wanted = {k: v for k, v in filters.items() if v not in (None, "")}
        if "status" in wanted:
            if wanted["status"] not in STATUSES:
                raise ValueError(f"status must be one of {STATUSES}")
        with self._guard:
            cards = []
            for rec in self.vault.list():
                card = self._card({**rec, "content": None})
                if not can_see(party, card):
                    continue
                if any(card[k] != wanted[k] for k in ("recipient", "claimant", "status",
                                                      "kind", "reply_to") if k in wanted):
                    continue
                if "sender" in wanted and card["sender"]["swarm"] != wanted["sender"]:
                    continue
                full = self.vault.read(card["id"])
                if full is not None:
                    card = self._card(full)
                cards.append(card)
        cards.sort(key=lambda c: (c["created_at"], c["id"]))
        return cards

    # -- the watcher -----------------------------------------------------------

    def sweep(self) -> list[dict]:
        """Notice cards whose `#status` changed outside this process, and emit `card.moved`.

        Compares each card note with the status last recorded for it. A card
        seen for the first time is recorded without an event. A drag back to
        Posted releases the claim, because a card nobody holds should be
        claimable again. Returns the events it emitted.
        """
        moved: list[dict] = []
        with self._guard:
            self.vault.board_id(refresh=True)
            recs = self.vault.list()
            live = set()
            for rec in recs:
                card_id = rec["labels"]["cardId"]
                live.add(card_id)
                column = rec["labels"].get("status") or ""
                status = _STATUS_OF.get(column, column.lower() or "posted")
                previous = self._seen.get(card_id)
                if previous is None:
                    self._remember(card_id, status)
                    continue
                if previous == status:
                    continue
                try:
                    moved.append(self._record_move(card_id, status))
                except VaultError as e:
                    log.warning("could not record the move of card %s: %s", card_id, e)
            stale = [c for c in self._seen if c not in live]
            for card_id in stale:
                del self._seen[card_id]
            self.events.seen_drop(stale)
        return moved

    def _record_move(self, card_id: str, status: str) -> dict:
        rec = self.vault.read(card_id)
        if rec is None:
            raise VaultError(f"card {card_id} vanished mid-sweep")
        card = self._card(rec)
        now = _now()
        labels = {"cardUpdated": now}
        if status == "posted" and card["claimant"]:
            labels["cardClaimant"] = ""
            card["claimant"] = None
        self.vault.update(rec["note_id"], labels=labels)
        card.update(status=status, updated_at=now)
        event_id = self.events.append("card.moved", card)
        self._remember(card_id, status)
        return {"id": event_id, "type": "card.moved", "card": card}

    def watch(self, stop: threading.Event, interval: float = 5.0) -> None:
        """Run `sweep` every ``interval`` seconds until ``stop`` is set, pruning old events hourly."""
        pruned = 0.0
        while not stop.is_set():
            try:
                self.sweep()
                if pruned <= 0:
                    self.events.prune()
                    pruned = 3600.0
            except Exception:
                log.exception("board sweep failed")
            pruned -= interval
            stop.wait(interval)
